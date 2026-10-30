#!/usr/bin/env python3
"""Profile a local audio corpus without changing the source files.

Requirements:
    Python 3.11+ (standard library only)
    FFmpeg's ffprobe executable available on PATH

Usage:
    python analyze_audio_corpus.py
    python analyze_audio_corpus.py --limit 10
    python analyze_audio_corpus.py --audio-dir "D:\\datasets\\audio\\audio_files"
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median


AUDIO_EXTENSIONS = {
    ".wav", ".flac", ".mp3", ".ogg", ".oga", ".opus",
    ".m4a", ".aac", ".aiff", ".aif", ".au", ".wma",
}

LOSSLESS_CODECS = {
    "flac", "alac", "wavpack", "ape", "tta", "shorten", "mlp", "truehd",
}
LOSSY_CODECS = {
    "mp3", "aac", "vorbis", "opus", "wmav1", "wmav2", "wmapro",
    "wmavoice", "ac3", "eac3", "mp2", "amr_nb", "amr_wb", "speex",
}

CSV_COLUMNS = [
    "relative_path", "extension", "size_bytes", "status", "error", "warnings",
    "container", "codec", "compression", "sample_rate_hz", "channels",
    "channel_layout", "sample_format", "reported_bits_per_sample",
    "duration_seconds", "bitrate_kbps", "audio_streams", "video_streams",
    "attached_pictures", "has_embedded_tags",
]


def parse_positive_int(value: object) -> int | None:
    """Return an integer only if the value is a positive number."""
    try:
        result = int(value)  # type: ignore[arg-type]
        return result if result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_positive_float(value: object) -> float | None:
    """Return a finite positive float; ffprobe may output 'N/A'."""
    try:
        result = float(value)  # type: ignore[arg-type]
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def compression_type(codec: str) -> str:
    """Classify the reported codec, leaving unrecognized codecs as unknown."""
    if codec.startswith("pcm_") or codec in LOSSLESS_CODECS:
        return "lossless"
    if codec in LOSSY_CODECS or codec.startswith("adpcm_"):
        return "lossy"
    return "unknown"


def probe_file(path: Path, root: Path, ffprobe: str, timeout: int) -> dict:
    """Read container and stream headers with ffprobe, without decoding audio."""
    record = dict.fromkeys(CSV_COLUMNS, "")
    record.update(
        relative_path=path.relative_to(root).as_posix(),
        extension=path.suffix.lower(),
        status="ok",
    )

    try:
        record["size_bytes"] = path.stat().st_size
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_format", "-show_streams",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        record.update(status="timeout", error=f"ffprobe exceeded {timeout} seconds")
        return record
    except OSError as exc:
        record.update(status="file_error", error=str(exc)[:400])
        return record

    if result.returncode != 0:
        record.update(status="probe_error", error=result.stderr.strip()[:400])
        return record

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        record.update(status="invalid_probe_json", error="ffprobe returned invalid JSON")
        return record

    streams = info.get("streams", [])
    file_info = info.get("format", {})
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    cover_art = [
        s for s in video_streams if s.get("disposition", {}).get("attached_pic") == 1
    ]
    normal_video = len(video_streams) - len(cover_art)
    record["audio_streams"] = len(audio_streams)
    record["video_streams"] = normal_video
    record["attached_pictures"] = len(cover_art)
    record["container"] = file_info.get("format_name", "")
    record["has_embedded_tags"] = bool(file_info.get("tags"))

    if not audio_streams:
        record.update(status="no_audio_stream", error="No audio stream was found")
        return record

    stream = audio_streams[0]
    codec = str(stream.get("codec_name") or "unknown").lower()
    record["codec"] = codec
    record["compression"] = compression_type(codec)
    record["sample_rate_hz"] = parse_positive_int(stream.get("sample_rate")) or ""
    record["channels"] = parse_positive_int(stream.get("channels")) or ""
    record["channel_layout"] = stream.get("channel_layout") or ""
    record["sample_format"] = stream.get("sample_fmt") or ""
    record["reported_bits_per_sample"] = (
        parse_positive_int(stream.get("bits_per_raw_sample"))
        or parse_positive_int(stream.get("bits_per_sample"))
        or ""
    )

    # Some containers report duration only at the file level.
    record["duration_seconds"] = (
        parse_positive_float(stream.get("duration"))
        or parse_positive_float(file_info.get("duration"))
        or ""
    )
    bitrate = (
        parse_positive_int(stream.get("bit_rate"))
        or parse_positive_int(file_info.get("bit_rate"))
    )
    record["bitrate_kbps"] = round(bitrate / 1000, 2) if bitrate else ""

    warnings = []
    if len(audio_streams) > 1:
        warnings.append("multiple_audio_streams")
    if normal_video:
        warnings.append("contains_video_stream")
    if not record["duration_seconds"]:
        warnings.append("missing_duration")
    if not record["sample_rate_hz"]:
        warnings.append("missing_sample_rate")
    if not record["channels"]:
        warnings.append("missing_channels")
    if record["size_bytes"] == 0:
        warnings.append("empty_file")
    record["warnings"] = ";".join(warnings)
    return record


def percentile(values: list[float], percentile_rank: float) -> float | None:
    """Calculate a linearly interpolated percentile without external packages."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_rank / 100
    left = math.floor(position)
    right = math.ceil(position)
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def stats(values: list[float], decimals: int = 2) -> dict:
    """Return the quantiles used to decide formatting and processing policy."""
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), decimals),
        "p25": round(percentile(values, 25), decimals),
        "median": round(median(values), decimals),
        "p75": round(percentile(values, 75), decimals),
        "p90": round(percentile(values, 90), decimals),
        "p95": round(percentile(values, 95), decimals),
        "p99": round(percentile(values, 99), decimals),
        "max": round(max(values), decimals),
    }


def frequency(rows: list[dict], field: str) -> dict[str, int]:
    """Count values, explicitly marking values missing from ffprobe."""
    counts = Counter(str(row[field]) if row[field] != "" else "missing" for row in rows)
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def summarize(rows: list[dict], root: Path, ffprobe_version: str) -> dict:
    """Create a concise report for decisions on codec, rate, channels, and size."""
    valid = [row for row in rows if row["status"] == "ok"]
    durations = [float(row["duration_seconds"]) for row in valid if row["duration_seconds"] != ""]
    bitrates = [float(row["bitrate_kbps"]) for row in valid if row["bitrate_kbps"] != ""]
    sizes_mib = [int(row["size_bytes"]) / (1024 ** 2) for row in valid]
    duration_buckets = Counter()
    pcm16_bytes = 0.0
    pcm16_estimated_count = 0
    duration_by_rate = Counter()
    duration_by_channels = Counter()
    size_by_compression = Counter()

    for row in valid:
        size_by_compression[str(row["compression"])] += int(row["size_bytes"])
        dur = row["duration_seconds"]
        if dur != "":
            seconds = float(dur)
            duration_by_rate[str(row["sample_rate_hz"] or "missing")] += seconds
            duration_by_channels[str(row["channels"] or "missing")] += seconds
            if seconds < 10:
                duration_buckets["under_10s"] += 1
            elif seconds < 30:
                duration_buckets["10_to_30s"] += 1
            elif seconds < 60:
                duration_buckets["30_to_60s"] += 1
            elif seconds < 180:
                duration_buckets["1_to_3min"] += 1
            elif seconds < 600:
                duration_buckets["3_to_10min"] += 1
            elif seconds < 1800:
                duration_buckets["10_to_30min"] += 1
            else:
                duration_buckets["over_30min"] += 1
            if row["sample_rate_hz"] != "" and row["channels"] != "":
                # Approximate PCM16 disk usage at each file's original rate/channels.
                pcm16_bytes += seconds * int(row["sample_rate_hz"]) * int(row["channels"]) * 2
                pcm16_estimated_count += 1

    return {
        "analyzed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_folder_name": root.name,
        "tool": "ffprobe",
        "ffprobe_version": ffprobe_version,
        "method": "container/stream metadata inspection (no full audio decoding)",
        "files_scanned": len(rows),
        "files_probed_ok": len(valid),
        "files_with_issues": sum(bool(r["warnings"]) or r["status"] != "ok" for r in rows),
        "counts_by_status": frequency(rows, "status"),
        "total_source_size_gib": round(sum(int(r["size_bytes"] or 0) for r in rows) / (1024 ** 3), 3),
        "total_known_duration_hours": round(sum(durations) / 3600, 2),
        "duration_seconds": stats(durations),
        "duration_distribution": dict(sorted(duration_buckets.items())),
        "file_size_mib": stats(sizes_mib),
        "bitrate_kbps": stats(bitrates),
        "by_extension": frequency(rows, "extension"),
        "by_container": frequency(valid, "container"),
        "by_codec": frequency(valid, "codec"),
        "by_compression": frequency(valid, "compression"),
        "by_sample_rate_hz": frequency(valid, "sample_rate_hz"),
        "by_channels": frequency(valid, "channels"),
        "by_channel_layout": frequency(valid, "channel_layout"),
        "by_sample_format": frequency(valid, "sample_format"),
        "by_reported_bits_per_sample": frequency(valid, "reported_bits_per_sample"),
        "hours_by_sample_rate_hz": {
            k: round(v / 3600, 2) for k, v in sorted(duration_by_rate.items())
        },
        "hours_by_channels": {
            k: round(v / 3600, 2) for k, v in sorted(duration_by_channels.items())
        },
        "source_size_gib_by_compression": {
            k: round(v / (1024 ** 3), 2) for k, v in sorted(size_by_compression.items())
        },
        "with_embedded_tags": sum(bool(r["has_embedded_tags"]) for r in valid),
        "with_multiple_audio_streams": sum(int(r["audio_streams"] or 0) > 1 for r in valid),
        "with_attached_picture": sum(int(r["attached_pictures"] or 0) > 0 for r in valid),
        "estimated_pcm16_gib_original_rate_channels": round(pcm16_bytes / (1024 ** 3), 2),
        "files_in_pcm16_estimate": pcm16_estimated_count,
        "notes": [
            "A successful ffprobe result does not prove the whole audio file decodes correctly.",
            "sample_format describes the decoder output, not necessarily the source bit depth.",
            "PCM16 is a theoretical uncompressed estimate, not a prediction of FLAC size.",
            "Embedded tags are not the external JSONL manifest and are not required for formatting.",
        ],
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write a spreadsheet-friendly CSV using stable column order."""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    """Select the dataset folder, run ffprobe, and write local diagnostic reports."""
    script_dir = Path(__file__).resolve().parent
    default_dir = script_dir / "audio_files" if (script_dir / "audio_files").is_dir() else script_dir

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-dir", type=Path, default=default_dir,
                        help="Audio folder; defaults to ./audio_files if present, otherwise the script folder")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Report folder; defaults to ./audio_profile_results beside the script")
    parser.add_argument("--limit", type=int, default=0,
                        help="Analyze only the first N audio files (useful for a quick test)")
    parser.add_argument("--timeout", type=int, default=60,
                        help="Maximum seconds per ffprobe call (default: 60)")
    args = parser.parse_args()

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        print("ERROR: ffprobe was not found. Install FFmpeg and restart PowerShell.", file=sys.stderr)
        return 2

    if args.limit < 0 or args.timeout <= 0:
        parser.error("--limit must be >= 0 and --timeout must be > 0")
    root = args.audio_dir.resolve()
    if not root.is_dir():
        print(f"ERROR: Audio directory does not exist: {root}", file=sys.stderr)
        return 2

    out_dir = (args.output_dir or script_dir / "audio_profile_results").resolve()
    audio_files = sorted(
        (p for p in root.rglob("*") if p.is_file() and not p.is_symlink()
         and p.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda p: str(p).lower(),
    )
    if not audio_files:
        print(f"ERROR: No supported audio files found under {root}", file=sys.stderr)
        return 2

    if args.limit:
        audio_files = audio_files[:args.limit]
    version_result = subprocess.run([ffprobe, "-version"], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", check=False)
    ffprobe_version = version_result.stdout.splitlines()[0] if version_result.stdout else "unknown"

    print(f"Audio folder : {root}")
    print(f"Audio files  : {len(audio_files)}")
    print(f"Report folder: {out_dir}")
    print("Method       : metadata inspection only (no conversion or modification)")
    rows = []
    for index, path in enumerate(audio_files, start=1):
        row = probe_file(path, root, ffprobe, args.timeout)
        rows.append(row)
        if index == 1 or index % 50 == 0 or index == len(audio_files):
            print(f"Processed {index}/{len(audio_files)} | latest status: {row['status']}", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    inventory_file = out_dir / "audio_inventory.csv"
    issues_file = out_dir / "audio_issues.csv"
    summary_file = out_dir / "audio_summary.json"
    write_csv(inventory_file, rows)
    write_csv(issues_file, [r for r in rows if r["status"] != "ok" or r["warnings"]])
    summary = summarize(rows, root, ffprobe_version)
    with summary_file.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(f"\nFinished: {summary['files_probed_ok']}/{summary['files_scanned']} files probed successfully")
    print(f"Total known duration: {summary['total_known_duration_hours']} hours")
    print(f"Source size: {summary['total_source_size_gib']} GiB")
    print(f"Files with errors/warnings: {summary['files_with_issues']}")
    print(f"Summary  : {summary_file}")
    print(f"Inventory: {inventory_file}")
    print(f"Issues   : {issues_file}")
    return 0 if summary["files_probed_ok"] == summary["files_scanned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

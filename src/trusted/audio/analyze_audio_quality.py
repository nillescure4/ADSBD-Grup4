"""Profile full-length FLAC objects in MinIO Formatted without modifying them.

The first Trusted step measures data quality before adopting a cleaning policy.
Each object is downloaded and inspected independently. Metrics are advisory:
a quality flag never causes an automatic deletion or modification.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src.common.config import load_storage_config
from src.common.logging import configure_logging
from src.common.minio_client import create_minio_client, list_object_keys


LOGGER = logging.getLogger(__name__)
SOURCE_PREFIX = "audio/"
SILENCE_THRESHOLD_DB = -60
MIN_SILENCE_SECONDS = 2
REPORT_COLUMNS = [
    "status", "source_key", "source_etag", "size_bytes", "sha256",
    "duration_seconds", "codec", "sample_rate_hz", "channels", "bit_depth",
    "peak_dbfs", "rms_dbfs", "dc_offset", "silence_seconds", "silence_ratio",
    "duplicate_of", "flags", "error",
]


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Execute FFmpeg utilities safely without a shell."""
    return subprocess.run(args, capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace")


def require_tools() -> None:
    """Check local availability of the external audio inspection tools."""
    for tool in ("ffprobe", "ffmpeg"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} was not found on PATH")


def parse_float(value: Any) -> float | None:
    """Return finite numeric values; ignore missing or non-finite measurements."""
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def probe_flac(path: Path) -> dict[str, Any]:
    """Read container and audio-stream properties from FFprobe."""
    result = run_command([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=codec_name,sample_rate,channels,bits_per_raw_sample,bits_per_sample:format=duration",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        raise ValueError(f"FFprobe failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        duration = float(data["format"]["duration"])
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample"))
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ValueError("Missing or invalid FLAC stream properties") from exc
    codec = stream.get("codec_name")
    if codec != "flac" or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Expected FLAC audio with positive duration")
    if sample_rate <= 0 or channels <= 0 or bits not in (16, 24):
        raise ValueError("Invalid or unexpected FLAC encoding properties")
    return {
        "codec": codec, "sample_rate_hz": sample_rate, "channels": channels,
        "bit_depth": bits, "duration_seconds": duration,
    }


def parse_audio_metrics(output: str, duration: float) -> dict[str, float | None]:
    """Parse whole-file FFmpeg astats and silence-detection diagnostics."""
    # Use only the final Overall block; per-channel metrics are not equivalent.
    overall_parts = re.split(r"\bOverall\s*\n", output)
    if len(overall_parts) < 2:
        raise ValueError("FFmpeg did not provide overall audio statistics")
    overall = overall_parts[-1]

    def metric(label: str) -> float | None:
        match = re.search(rf"\b{re.escape(label)}:\s*([^\s]+)", overall)
        return parse_float(match.group(1)) if match else None

    peak = metric("Peak level dB")
    rms = metric("RMS level dB")
    dc = metric("DC offset")
    # FFmpeg reports -inf dBFS for digital silence; record the condition explicitly.
    if peak is None and re.search(r"\bPeak level dB:\s*-inf\b", overall, re.I):
        peak = float("-inf")
    if rms is None and re.search(r"\bRMS level dB:\s*-inf\b", overall, re.I):
        rms = float("-inf")
    if peak is None or rms is None or dc is None:
        raise ValueError("FFmpeg did not provide peak, RMS, and DC offset values")

    durations = [float(value) for value in re.findall(
        r"silence_end:\s*[\d.]+\s*\|\s*silence_duration:\s*([\d.]+)", output
    )]
    silent_seconds = min(duration, sum(durations))
    return {
        "peak_dbfs": peak, "rms_dbfs": rms, "dc_offset": dc,
        "silence_seconds": silent_seconds,
        "silence_ratio": silent_seconds / duration,
    }


def inspect_signal(path: Path, duration: float) -> dict[str, float | None]:
    """Fully decode audio to measure amplitude, DC bias, and prolonged silence."""
    result = run_command([
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
        "-xerror", "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-af", f"astats=metadata=0:reset=0,silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={MIN_SILENCE_SECONDS}",
        "-f", "null", "-",
    ])
    if result.returncode != 0:
        raise ValueError(f"Audio decode failed: {result.stderr.strip()[-400:]}")
    return parse_audio_metrics(result.stderr, duration)


def sha256_file(path: Path) -> str:
    """Hash an object without loading the complete file into RAM."""
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def classify(row: dict[str, str]) -> str:
    """Assign review-only flags; these thresholds are not deletion rules."""
    if row["status"] == "invalid":
        return "invalid"
    flags: list[str] = []
    duration = float(row["duration_seconds"])
    peak = float(row["peak_dbfs"])
    dc = float(row["dc_offset"])
    silence_ratio = float(row["silence_ratio"])
    if duration < 1:
        flags.append("very_short")
    if peak == float("-inf"):
        flags.append("digital_silence")
    elif peak > -0.1:
        flags.append("near_full_scale")
    if abs(dc) > 0.01:
        flags.append("dc_offset")
    if silence_ratio > 0.30:
        flags.append("prolonged_silence")
    if row["duplicate_of"]:
        flags.append("identical_file")
    row["flags"] = ";".join(flags)
    row["status"] = "review" if flags else "ok"
    return row["status"]


def profile_object(client: Any, bucket: str, key: str,
                   duplicate_hashes: dict[str, str]) -> dict[str, str]:
    """Download and profile one Formatted object, preserving its source."""
    row = {field: "" for field in REPORT_COLUMNS}
    row["source_key"] = key
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        row["source_etag"] = str(head.get("ETag", "")).strip('"')
        row["size_bytes"] = str(head["ContentLength"])
        with tempfile.TemporaryDirectory(prefix="adsdb-trusted-profile-") as tempdir:
            path = Path(tempdir) / "input.flac"
            client.download_file(bucket, key, str(path))
            if path.stat().st_size != head["ContentLength"]:
                raise ValueError("Downloaded object size differs from MinIO metadata")
            row["sha256"] = sha256_file(path)
            probe = probe_flac(path)
            metrics = inspect_signal(path, probe["duration_seconds"])
            for field, value in {**probe, **metrics}.items():
                row[field] = str(value)
        row["duplicate_of"] = duplicate_hashes.get(row["sha256"], "")
        duplicate_hashes.setdefault(row["sha256"], key)
        row["status"] = "ok"
        classify(row)
    except (ValueError, KeyError) as exc:
        row["status"] = "invalid"
        row["error"] = str(exc)[:400]
    except (ClientError, BotoCoreError, OSError, RuntimeError) as exc:
        row["status"] = "failed"
        row["error"] = str(exc)[:400]
    return row


def run_analysis(client: Any, storage: dict[str, Any], *,
                 limit: int | None = None, dry_run: bool = False,
                 report: Path | None = None) -> dict[str, int]:
    """Create a CSV quality profile for MinIO Formatted audio objects."""
    if limit is not None and limit <= 0:
        raise ValueError("--limit must be positive")
    bucket = storage["buckets"]["formatted"]
    keys = sorted(key for key in list_object_keys(client, bucket, SOURCE_PREFIX)
                  if key.startswith(SOURCE_PREFIX) and key.lower().endswith(".flac"))
    selected = keys[:limit] if limit is not None else keys
    LOGGER.info("Found %d FLAC objects under s3://%s/%s; selected %d",
                len(keys), bucket, SOURCE_PREFIX, len(selected))
    counts = {"selected": len(selected), "ok": 0, "review": 0, "invalid": 0, "failed": 0, "planned": 0}
    if dry_run:
        for key in selected:
            LOGGER.info("PLAN %s", key)
        counts["planned"] = len(selected)
        return counts
    if not selected:
        return counts
    require_tools()
    output = report or Path("reports") / "audio_trusted_quality.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    duplicates: dict[str, str] = {}
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for index, key in enumerate(selected, start=1):
            row = profile_object(client, bucket, key, duplicates)
            counts[row["status"]] += 1
            writer.writerow(row)
            stream.flush()
            LOGGER.info("[%d/%d] %s: %s", index, len(selected), row["status"].upper(), key)
            if row["flags"]:
                LOGGER.warning("  Quality flags: %s", row["flags"])
            if row["error"]:
                LOGGER.error("  %s", row["error"])
    LOGGER.info("Quality report written to %s", output)
    return counts


def main() -> int:
    """Run non-destructive quality profiling from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="Maximum number of FLAC files")
    parser.add_argument("--dry-run", action="store_true", help="List selected objects without reading audio")
    parser.add_argument("--report", type=Path, help="Output CSV report (overwrites any existing file)")
    args = parser.parse_args()
    configure_logging()
    try:
        counts = run_analysis(create_minio_client(), load_storage_config(),
                              limit=args.limit, dry_run=args.dry_run, report=args.report)
    except (ValueError, RuntimeError, OSError) as exc:
        LOGGER.error("Trusted quality analysis could not start: %s", exc)
        return 1
    LOGGER.info("Summary: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 2 if counts["invalid"] or counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

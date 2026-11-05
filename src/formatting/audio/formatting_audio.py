"""Read original audio from MinIO Landing Persistent and publish full-length FLAC.

Processing uses one temporary local file at a time. No segmentation, resampling,
channel remixing, or quality filtering is performed in this zone.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from src.common.config import load_storage_config
from src.common.logging import configure_logging
from src.common.minio_client import create_minio_client, list_object_keys


AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".au", ".flac", ".m4a",
    ".mp3", ".oga", ".ogg", ".opus", ".wav", ".wma",
}
LOSSY_CODECS = {"aac", "mp3", "opus", "vorbis", "wmav1", "wmav2"}
AUDIO_PREFIX = "audio"


@dataclass(frozen=True)
class AudioInfo:
    """Technical properties of the first audio stream in a file."""

    codec: str
    sample_rate: int
    channels: int
    bit_depth: int | None
    sample_format: str | None
    duration_seconds: float | None


def require_tools() -> None:
    """Ensure FFmpeg and FFprobe are available before processing any files."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} was not found on PATH. Install FFmpeg first.")


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an external media command without using a shell."""
    return subprocess.run(args, capture_output=True, text=True, check=False)


def as_integer(value: Any) -> int | None:
    """Convert FFprobe optional integer fields while handling N/A values."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def probe_audio(path: Path) -> AudioInfo:
    """Inspect the first audio stream rather than guessing from the extension."""
    result = run_command([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample,sample_fmt:format=duration",
        "-of", "json", str(path),
    ])
    if result.returncode != 0:
        raise ValueError(f"FFprobe error: {result.stderr.strip()}")

    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("No readable audio stream found") from exc

    rate = as_integer(stream.get("sample_rate"))
    channels = as_integer(stream.get("channels"))
    if rate is None or channels is None:
        raise ValueError("Missing sample rate or channel count")

    raw_bits = as_integer(stream.get("bits_per_raw_sample"))
    bits = raw_bits or as_integer(stream.get("bits_per_sample"))
    duration_raw = payload.get("format", {}).get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        duration = None

    return AudioInfo(
        codec=str(stream.get("codec_name", "unknown")),
        sample_rate=rate,
        channels=channels,
        bit_depth=bits,
        sample_format=stream.get("sample_fmt"),
        duration_seconds=duration,
    )


def choose_flac_bits(info: AudioInfo) -> int:
    """Select 16/24-bit FLAC with an explicit policy for 32-bit PCM.

    Lossy codecs are decoded to 16-bit PCM. Integer PCM through 24 bits keeps
    its precision. Float32 and signed integer PCM32 are converted to 24-bit
    integer FLAC, since the FFmpeg FLAC encoder uses integer samples.
    """
    codec = info.codec.lower()
    if codec in LOSSY_CODECS:
        return 16
    if codec.startswith(("pcm_f32", "pcm_s32")):
        return 24
    if codec.startswith("pcm_") or codec == "flac":
        if info.bit_depth is None:
            raise ValueError(f"Original bit depth is unknown for {codec}")
        if info.bit_depth <= 16:
            return 16
        if info.bit_depth <= 24:
            return 24
        raise ValueError(f"Unsupported precision: {codec}, {info.bit_depth} bits")
    raise ValueError(f"Unsupported or unclassified audio codec: {codec}")


def inspect_float_peak_dbfs(source: Path) -> float:
    """Measure the complete float audio stream before integer quantization.

    FFmpeg astats reports its overall peak and the number of non-finite samples.
    A missing or invalid measurement is rejected rather than risking clipping.
    """
    result = run_command([
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
        "-xerror", "-i", str(source), "-map", "0:a:0",
        "-vn", "-sn", "-dn", "-af", "astats=metadata=0:reset=0",
        "-f", "null", "-",
    ])
    if result.returncode != 0:
        raise ValueError(f"Float32 analysis failed: {result.stderr.strip()}")

    # The final astats section is labeled Overall and covers all channels.
    sections = result.stderr.split(" Overall\n")
    if len(sections) < 2:
        raise ValueError("Float32 analysis did not provide overall statistics")
    overall = sections[-1]
    for label in ("Number of NaNs", "Number of Infs"):
        match = re.search(rf"{label}:\s*([^\s]+)", overall)
        if match is None:
            raise ValueError(f"Float32 analysis did not report {label}")
        try:
            count = float(match.group(1))
        except ValueError as exc:
            raise ValueError(f"Invalid {label} from FFmpeg") from exc
        if not math.isfinite(count) or count > 0:
            raise ValueError(f"Float32 contains invalid samples: {label}={count}")

    match = re.search(r"Peak level dB:\s*([^\s]+)", overall)
    if match is None:
        raise ValueError("Float32 analysis did not report an overall peak")
    try:
        peak_dbfs = float(match.group(1))
    except ValueError as exc:
        raise ValueError("Invalid float32 peak reported by FFmpeg") from exc
    if math.isnan(peak_dbfs) or peak_dbfs == math.inf:
        raise ValueError("Float32 peak measurement is not finite")
    # -inf dBFS is a valid peak for a silent audio stream.
    return peak_dbfs


def flac_conversion_settings(source: Path, info: AudioInfo) -> tuple[int, list[str], str]:
    """Choose FLAC precision, clipping-safe filters and an auditable note."""
    bits = choose_flac_bits(info)
    codec = info.codec.lower()
    if codec.startswith("pcm_s32"):
        return bits, [], "PCM32 integer reduced to 24-bit FLAC; precision loss is expected"
    if codec.startswith("pcm_f32") or codec in LOSSY_CODECS:
        peak = inspect_float_peak_dbfs(source)
        if peak > 0.0:
            # Keep 0.1 dB of headroom for safe floating-to-integer rounding.
            gain = -peak - 0.1
            filter_arg = f"volume={gain:.9f}dB:precision=float"
            note = (
                f"{codec} decoded to {bits}-bit FLAC; source peak={peak:.6f} dBFS; "
                f"applied gain={gain:.6f} dB to prevent clipping"
            )
            return bits, ["-af", filter_arg], note
        peak_text = "silence" if peak == -math.inf else f"{peak:.6f} dBFS"
        return bits, [], (
            f"{codec} decoded to {bits}-bit FLAC; source peak={peak_text}; "
            "no gain applied; precision loss is expected"
        )
    return bits, [], ""


def convert_audio(source: Path, destination: Path, *, overwrite: bool = False) -> tuple[str, AudioInfo | None, int | None, str]:
    """Convert a complete audio file and verify the FLAC technical properties."""
    if destination.exists() and not overwrite:
        return "skipped", None, None, "Destination exists (source revision not checked)"

    info = probe_audio(source)
    output_bits, filters, conversion_note = flac_conversion_settings(source, info)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Publish local output only after FFmpeg finishes and checks pass.
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".audio-part-", suffix=".flac", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-xerror", "-y", "-i", str(source),
            "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
            "-vn", "-sn", "-dn",
            *filters,
            "-c:a", "flac", "-compression_level", "5",
            "-sample_fmt", "s16" if output_bits == 16 else "s32",
            "-bits_per_raw_sample", str(output_bits),
            str(temporary),
        ]
        result = run_command(command)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg error: {result.stderr.strip()}")
        formatted = probe_audio(temporary)
        if (
            formatted.codec != "flac"
            or formatted.sample_rate != info.sample_rate
            or formatted.channels != info.channels
            or formatted.bit_depth != output_bits
        ):
            raise RuntimeError("Converted file failed the FLAC property checks")
        os.replace(temporary, destination)
        return "converted", info, output_bits, conversion_note
    finally:
        temporary.unlink(missing_ok=True)


LOGGER = logging.getLogger(__name__)
MINIO_REPORT_COLUMNS = [
    "status", "source_bucket", "source_key", "formatted_bucket", "formatted_key",
    "source_bytes", "source_sha256", "source_codec", "sample_rate_hz",
    "channels", "source_bit_depth", "formatted_bit_depth", "duration_seconds",
    "formatted_bytes", "message",
]
FORMAT_VERSION = "audio-flac-v2"


def normalize_source_prefix(prefix: str, persistent_prefix: str) -> str:
    """Limit the input to a source below the configured persistent landing area."""
    normalized = prefix.strip("/")
    persistent = persistent_prefix.strip("/")
    if not normalized.startswith(persistent + "/"):
        raise ValueError(
            f"Source prefix must be below '{persistent}/', for example "
            f"'{persistent}/audio/'"
        )
    if any(segment in {"", ".", ".."} for segment in normalized.split("/")):
        raise ValueError("Source prefix contains an invalid path segment")
    return normalized + "/"


def destination_key_for(source_key: str, persistent_prefix: str) -> str:
    """Mirror the Landing Persistent hierarchy in Formatted without key collisions."""
    root = persistent_prefix.strip("/") + "/"
    if not source_key.startswith(root):
        raise ValueError("Source object is outside Landing Persistent")
    relative = source_key[len(root):]
    if not relative or any(segment in {"", ".", ".."} for segment in relative.split("/")):
        raise ValueError("Source object key has invalid path segments")
    # Preserve the input extension to avoid collisions such as song.mp3/song.wav.
    return relative + ".flac"


def hash_file(path: Path) -> str:
    """Stream SHA256 from disk without loading the whole audio into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_signature(bucket: str, key: str, head: dict[str, Any]) -> str:
    """Identify a specific MinIO source revision for incremental processing."""
    etag = str(head.get("ETag", "")).strip('"')
    recorded_sha = str(head.get("Metadata", {}).get("sha256", ""))
    if not etag and not recorded_sha:
        raise ValueError("Source lacks ETag and SHA256 metadata; cannot track revisions")
    modified = head.get("LastModified")
    fields = [
        bucket, key, etag, recorded_sha, str(head.get("ContentLength", "")),
        modified.isoformat() if modified is not None else "",
    ]
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def head_if_exists(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    """Return None only for a missing object; propagate permissions/network errors."""
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def plan_audio_objects(keys: list[str], source_prefix: str, limit: int | None) -> list[str]:
    """Pick only audio objects from the configured source, never sidecar manifests."""
    selected = sorted(
        key for key in keys
        if key.startswith(source_prefix) and Path(key).suffix.lower() in AUDIO_EXTENSIONS
    )
    return selected[:limit] if limit is not None else selected


def process_one(
    client: Any,
    source_bucket: str,
    formatted_bucket: str,
    source_key: str,
    persistent_prefix: str,
    *,
    overwrite: bool,
) -> dict[str, str]:
    """Download, convert, upload, and verify one MinIO audio object."""
    formatted_key = destination_key_for(source_key, persistent_prefix)
    row = {name: "" for name in MINIO_REPORT_COLUMNS}
    row.update({
        "source_bucket": source_bucket,
        "source_key": source_key,
        "formatted_bucket": formatted_bucket,
        "formatted_key": formatted_key,
    })
    try:
        original = client.head_object(Bucket=source_bucket, Key=source_key)
        signature = source_signature(source_bucket, source_key, original)
        row["source_bytes"] = str(original["ContentLength"])

        previous = head_if_exists(client, formatted_bucket, formatted_key)
        if previous is not None:
            previous_sig = previous.get("Metadata", {}).get("source_signature")
            previous_version = previous.get("Metadata", {}).get("format_version")
            if previous_sig == signature and previous_version == FORMAT_VERSION and not overwrite:
                row["status"] = "skipped"
                row["message"] = "Current formatted version already exists"
                return row
            tracked_upgrade = (
                previous_sig == signature and previous_version == "audio-flac-v1"
                and previous.get("Metadata", {}).get("source_key_sha256") == hashlib.sha256(source_key.encode("utf-8")).hexdigest()
                and bool(previous.get("Metadata", {}).get("source_sha256"))
            )
            if not overwrite and not tracked_upgrade:
                raise RuntimeError(
                    "Different or untracked destination exists; use --overwrite only after review"
                )

        with tempfile.TemporaryDirectory(prefix="adsbd-audio-") as workdir:
            folder = Path(workdir)
            # Use a neutral filename; the stream codec is detected from its bytes.
            source_file = folder / ("original" + Path(source_key).suffix.lower())
            formatted_file = folder / "formatted.flac"
            client.download_file(source_bucket, source_key, str(source_file))
            if source_file.stat().st_size != original["ContentLength"]:
                raise RuntimeError("Downloaded size differs from MinIO source size")
            downloaded_sha = hash_file(source_file)
            row["source_sha256"] = downloaded_sha
            if previous is not None and tracked_upgrade and downloaded_sha != previous["Metadata"]["source_sha256"]:
                raise RuntimeError("Tracked legacy output refers to different original bytes")
            known_sha = original.get("Metadata", {}).get("sha256")
            if known_sha and downloaded_sha.lower() != known_sha.lower():
                raise RuntimeError("Downloaded audio differs from source SHA256 metadata")

            status, info, bits, message = convert_audio(source_file, formatted_file)
            if status != "converted" or info is None or bits is None:
                raise RuntimeError(f"Conversion did not complete: {status}: {message}")
            row.update({
                "message": message,
                "source_codec": info.codec,
                "sample_rate_hz": str(info.sample_rate),
                "channels": str(info.channels),
                "source_bit_depth": str(info.bit_depth or ""),
                "formatted_bit_depth": str(bits),
                "duration_seconds": (
                    f"{info.duration_seconds:.3f}" if info.duration_seconds is not None else ""
                ),
                "formatted_bytes": str(formatted_file.stat().st_size),
            })

            # Refuse to attach provenance of an outdated source revision.
            latest = client.head_object(Bucket=source_bucket, Key=source_key)
            if source_signature(source_bucket, source_key, latest) != signature:
                raise RuntimeError("Source object changed while being processed")

            # The shared upload_file helper does not yet accept S3 metadata.
            # Reuse its client and call the underlying boto3 upload with metadata.
            client.upload_file(
                str(formatted_file), formatted_bucket, formatted_key,
                ExtraArgs={"Metadata": {
                    "source_signature": signature,
                    "source_sha256": downloaded_sha,
                    "source_key_sha256": hashlib.sha256(source_key.encode("utf-8")).hexdigest(),
                    "format_version": FORMAT_VERSION,
                    "conversion_note": message,
                }},
            )
            published = client.head_object(Bucket=formatted_bucket, Key=formatted_key)
            if (
                published.get("ContentLength") != formatted_file.stat().st_size
                or published.get("Metadata", {}).get("source_signature") != signature
            ):
                raise RuntimeError("Uploaded FLAC failed size or provenance checks")

        row["status"] = "converted"
        return row
    except ValueError as exc:
        row["status"], row["message"] = "unsupported", str(exc)
    except (ClientError, OSError, RuntimeError) as exc:
        row["status"], row["message"] = "failed", str(exc)
    return row


def run_formatting(
    client: Any,
    storage: dict[str, Any],
    source_prefix: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
    report: Path | None = None,
) -> dict[str, int]:
    """Process MinIO audio objects incrementally and report each outcome."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be greater than zero")
    persistent = storage["landing"]["persistent_prefix"]
    normalized = normalize_source_prefix(source_prefix, persistent)
    source_bucket = storage["buckets"]["landing"]
    formatted_bucket = storage["buckets"]["formatted"]

    keys = list_object_keys(client, source_bucket, normalized)
    selected = plan_audio_objects(keys, normalized, limit)
    counts = {"selected": len(selected), "converted": 0, "skipped": 0,
              "unsupported": 0, "failed": 0, "planned": 0}
    LOGGER.info("Found %d audio objects under s3://%s/%s", len(selected), source_bucket, normalized)

    if dry_run:
        for key in selected:
            LOGGER.info("PLAN %s -> s3://%s/%s", key, formatted_bucket,
                        destination_key_for(key, persistent))
        counts["planned"] = len(selected)
        return counts

    if not selected:
        LOGGER.info("Nothing to process. Landing Persistent may not contain audio yet.")
        return counts

    require_tools()
    report_path = report if report is not None else Path("reports") / "audio_formatted_report.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MINIO_REPORT_COLUMNS)
        writer.writeheader()
        for index, key in enumerate(selected, 1):
            row = process_one(
                client, source_bucket, formatted_bucket, key, persistent,
                overwrite=overwrite,
            )
            counts[row["status"]] += 1
            writer.writerow(row)
            stream.flush()
            LOGGER.info("[%d/%d] %s: %s", index, len(selected), row["status"].upper(), key)
            if row["message"]:
                LOGGER.warning("  %s", row["message"])
    LOGGER.info("Processing report written to %s", report_path)
    return counts


def main() -> int:
    """Run the Formatted Zone directly against the configured MinIO instance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-prefix",
        help="Override the Landing Persistent audio prefix (default: configured persistent prefix + /audio/)",
    )
    parser.add_argument("--limit", type=int, help="Maximum number of audio objects")
    parser.add_argument("--dry-run", action="store_true", help="List planned objects without changing MinIO")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace existing formatted objects")
    parser.add_argument("--report", type=Path, help="Local CSV report path (not a source dataset)")
    args = parser.parse_args()
    configure_logging()
    try:
        storage = load_storage_config()
        # Resolve the input location from the shared storage configuration.
        persistent_prefix = storage["landing"]["persistent_prefix"].strip("/")
        source_prefix = args.source_prefix or f"{persistent_prefix}/{AUDIO_PREFIX}/"
        counts = run_formatting(
            create_minio_client(), storage, source_prefix,
            limit=args.limit, dry_run=args.dry_run,
            overwrite=args.overwrite, report=args.report,
        )
    except (ValueError, RuntimeError, ClientError) as exc:
        LOGGER.error("Audio formatting could not start: %s", exc)
        return 1
    LOGGER.info("Summary: %s", ", ".join(f"{name}={value}" for name, value in counts.items()))
    return 2 if counts["failed"] or counts["unsupported"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

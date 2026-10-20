"""Publish validated full-length FLAC audio from Formatted to Trusted in MinIO.

Duplicates are excluded using the previously verified full-corpus SHA256 report.
DC offset is corrected only when the source has enough peak headroom; output is
fully decoded and checked before publication. Other advisory flags are retained.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import math
import tempfile
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from src.common.config import load_storage_config
from src.common.logging import configure_logging
from src.common.minio_client import create_minio_client, list_object_keys
from src.trusted.audio.analyze_audio_quality import inspect_signal, probe_flac, require_tools, run_command, sha256_file
from src.trusted.audio.audio_quality_report import read_quality_report

LOGGER = logging.getLogger(__name__)
SOURCE_PREFIX = "audio/"
POLICY_VERSION = "audio-trusted-v1"
DC_THRESHOLD = 0.01
HEADROOM_MARGIN = 0.001
PROCESS_COLUMNS = [
    "status", "source_key", "trusted_key", "source_sha256", "method",
    "dc_offset_before", "dc_offset_after", "peak_dbfs_before", "peak_dbfs_after",
    "duplicate_of", "reason",
]


def source_row_valid(row: dict[str, str]) -> None:
    """Refuse malformed, failed, or non-audio report entries."""
    key = row["source_key"]
    relative = key[len(SOURCE_PREFIX):] if key.startswith(SOURCE_PREFIX) else ""
    if not relative or not key.lower().endswith(".flac") or any(
        part in {"", ".", ".."} for part in relative.split("/")
    ):
        raise ValueError(f"Invalid formatted source key: {key!r}")
    if row["status"] not in {"ok", "review"}:
        raise ValueError(f"Cannot publish {row['status']} object: {key}")
    sha = row.get("sha256", "")
    if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha.lower()):
        raise ValueError(f"Missing or invalid source SHA256: {key}")
    if not row.get("source_etag") or not row.get("size_bytes"):
        raise ValueError(f"Missing source revision identifiers: {key}")


def select_method(row: dict[str, str]) -> tuple[str, str]:
    """Plan conservative DC correction with proven whole-file peak headroom."""
    if row["duplicate_of"]:
        return "duplicate", "Exact file SHA256 already represented"
    flags = set(filter(None, row["flags"].split(";")))
    if "dc_offset" not in flags:
        return "copy", "Preserve content; no corrective action justified"
    try:
        dc = float(row["dc_offset"])
        peak = float(row["peak_dbfs"])
    except ValueError:
        return "copy", "Missing numerical evidence for safe DC correction"
    if not math.isfinite(dc) or not math.isfinite(peak):
        return "copy", "Invalid numerical evidence for safe DC correction"
    if abs(dc) <= DC_THRESHOLD:
        return "copy", "DC magnitude below correction threshold"
    linear_peak = 10 ** (peak / 20)
    if linear_peak + abs(dc) + HEADROOM_MARGIN >= 1.0:
        return "copy", "Insufficient headroom; manual review recommended"
    return "dcshift", "Apply conservative DC offset subtraction"


def metadata_for(row: dict[str, str], method: str, trusted_sha256: str | None = None) -> dict[str, str]:
    """Keep provenance and processing decisions on the output object."""
    metadata = {
        "trusted-policy": POLICY_VERSION,
        "source-sha256": row["sha256"],
        "source-key-sha256": hashlib.sha256(row["source_key"].encode("utf-8")).hexdigest(),
        "trusted-method": method,
    }
    if trusted_sha256:
        metadata["trusted-sha256"] = trusted_sha256
    return metadata


def head_optional(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    """Distinguish a missing output object from a real storage error."""
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def check_source_head(client: Any, bucket: str, row: dict[str, str]) -> dict[str, Any]:
    """Refuse a source that changed since the quality-analysis snapshot."""
    head = client.head_object(Bucket=bucket, Key=row["source_key"])
    if (str(head.get("ETag", "")).strip('"') != row["source_etag"]
            or int(head["ContentLength"]) != int(row["size_bytes"])):
        raise RuntimeError("Source changed after quality analysis; run analysis again")
    return head


def correct_dc(source: Path, output: Path, row: dict[str, str]) -> tuple[float, float]:
    """Correct global DC shift while preserving FLAC sample rate and channel count."""
    info = probe_flac(source)
    bits = int(info["bit_depth"])
    before_dc = float(row["dc_offset"])
    result = run_command([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
        "-i", str(source), "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
        "-vn", "-sn", "-dn", "-af", f"dcshift=shift={-before_dc:.12f}",
        "-c:a", "flac", "-compression_level", "5",
        "-sample_fmt", "s16" if bits == 16 else "s32", "-bits_per_raw_sample", str(bits),
        str(output),
    ])
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg DC correction failed: {result.stderr[-400:]}")
    after = probe_flac(output)
    if (after["codec"] != "flac" or after["sample_rate_hz"] != info["sample_rate_hz"]
            or after["channels"] != info["channels"] or after["bit_depth"] != bits
            or abs(after["duration_seconds"] - info["duration_seconds"]) > 0.01):
        raise RuntimeError("Corrected file changed essential audio properties")
    metrics = inspect_signal(output, after["duration_seconds"])
    after_dc = metrics["dc_offset"]
    after_peak = metrics["peak_dbfs"]
    if (after_dc is None or after_peak is None or not math.isfinite(after_dc)
            or not math.isfinite(after_peak) or abs(after_dc) > 0.01
            or abs(after_dc) >= abs(before_dc) * 0.75 or after_peak > 0.001):
        raise RuntimeError("Corrected file failed DC improvement or peak checks")
    return after_dc, after_peak


def process_one(client: Any, formatted_bucket: str, trusted_bucket: str,
                row: dict[str, str]) -> dict[str, str]:
    """Publish one verified audio object without overwriting unrelated outputs."""
    key = row["source_key"]
    outcome = {name: "" for name in PROCESS_COLUMNS}
    outcome.update({"source_key": key, "trusted_key": key,
                    "source_sha256": row["sha256"],
                    "dc_offset_before": row["dc_offset"],
                    "peak_dbfs_before": row["peak_dbfs"],
                    "duplicate_of": row["duplicate_of"]})
    try:
        check_source_head(client, formatted_bucket, row)
        method, reason = select_method(row)
        outcome.update({"method": method, "reason": reason})
        if method == "duplicate":
            outcome["status"] = "duplicate_excluded"
            return outcome
        existing = head_optional(client, trusted_bucket, key)
        if existing is not None:
            metadata = existing.get("Metadata", {})
            if (metadata.get("trusted-policy") == POLICY_VERSION
                    and metadata.get("source-sha256") == row["sha256"]
                    and metadata.get("trusted-method") == method
                    and int(existing["ContentLength"]) > 0):
                outcome["status"] = "skipped"
                return outcome
            raise RuntimeError("Different or untracked Trusted destination exists; refusing overwrite")
        if method == "copy":
            client.copy_object(
                Bucket=trusted_bucket, Key=key,
                CopySource={"Bucket": formatted_bucket, "Key": key},
                Metadata=metadata_for(row, "copy"), MetadataDirective="REPLACE",
                ContentType="audio/flac",
            )
            new = client.head_object(Bucket=trusted_bucket, Key=key)
            if (int(new["ContentLength"]) != int(row["size_bytes"])
                    or new.get("Metadata", {}).get("source-sha256") != row["sha256"]):
                raise RuntimeError("Copied object failed size or provenance checks")
            check_source_head(client, formatted_bucket, row)
            outcome["status"] = "copied"
            outcome["dc_offset_after"] = row["dc_offset"]
            outcome["peak_dbfs_after"] = row["peak_dbfs"]
        else:
            with tempfile.TemporaryDirectory(prefix="adsdb-trusted-clean-") as workspace:
                original = Path(workspace) / "input.flac"
                cleaned = Path(workspace) / "cleaned.flac"
                client.download_file(formatted_bucket, key, str(original))
                if (original.stat().st_size != int(row["size_bytes"])
                        or sha256_file(original).lower() != row["sha256"].lower()):
                    raise RuntimeError("Downloaded input differs from quality-report SHA256")
                after_dc, after_peak = correct_dc(original, cleaned, row)
                check_source_head(client, formatted_bucket, row)
                cleaned_sha = sha256_file(cleaned)
                client.upload_file(
                    str(cleaned), trusted_bucket, key,
                    ExtraArgs={"Metadata": metadata_for(row, "dcshift", cleaned_sha),
                               "ContentType": "audio/flac"},
                )
                verified = client.head_object(Bucket=trusted_bucket, Key=key)
                if (int(verified["ContentLength"]) != cleaned.stat().st_size
                        or verified.get("Metadata", {}).get("trusted-sha256") != cleaned_sha):
                    raise RuntimeError("Uploaded corrected FLAC failed integrity verification")
                outcome["status"] = "corrected"
                outcome["dc_offset_after"] = str(after_dc)
                outcome["peak_dbfs_after"] = str(after_peak)
    except (ClientError, BotoCoreError, OSError, RuntimeError, ValueError) as exc:
        outcome["status"] = "failed"
        outcome["reason"] = str(exc)[:400]
    return outcome


def run_trusted(client: Any, config: dict[str, Any], quality_report: Path, *,
                limit: int | None = None, dry_run: bool = False,
                report: Path | None = None) -> dict[str, int]:
    """Verify report coverage, select a safe plan, and publish Trusted audio."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be greater than zero")
    rows = read_quality_report(quality_report)
    indexed = {row["source_key"]: row for row in rows}
    for row in rows:
        source_row_valid(row)
        representative = row["duplicate_of"]
        if representative:
            owner = indexed[representative]
            if (owner["sha256"] != row["sha256"] or owner["duplicate_of"]):
                raise ValueError(f"Unverified duplicate relationship: {row['source_key']}")
    src_bucket = config["buckets"]["formatted"]
    dst_bucket = config["buckets"]["trusted"]
    present = {key for key in list_object_keys(client, src_bucket, SOURCE_PREFIX)
               if key.startswith(SOURCE_PREFIX) and key.lower().endswith(".flac")}
    recorded = {row["source_key"] for row in rows}
    if present != recorded:
        raise ValueError(f"Quality CSV does not cover current Formatted: "
                         f"missing={len(present - recorded)}, stale={len(recorded - present)}")
    rows = sorted(rows, key=lambda row: row["source_key"])
    selected = rows[:limit] if limit is not None else rows
    counts = {status: 0 for status in ("copied", "corrected", "duplicate_excluded", "skipped", "failed", "planned")}
    counts["selected"] = len(selected)
    LOGGER.info("Found %d matching Formatted files; selected %d", len(rows), len(selected))
    if dry_run:
        for row in selected:
            method, reason = select_method(row)
            LOGGER.info("PLAN %-9s %s (%s)", method.upper(), row["source_key"], reason)
        counts["planned"] = len(selected)
        return counts
    if any(select_method(row)[0] == "dcshift" for row in selected):
        require_tools()
    path = report or Path("reports") / "audio_trusted_processing.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PROCESS_COLUMNS)
        writer.writeheader()
        for index, row in enumerate(selected, 1):
            result = process_one(client, src_bucket, dst_bucket, row)
            counts[result["status"]] += 1
            writer.writerow(result)
            stream.flush()
            LOGGER.info("[%d/%d] %s: %s", index, len(selected), result["status"].upper(), row["source_key"])
            if result["status"] == "failed":
                LOGGER.error("  %s", result["reason"])
    LOGGER.info("Trusted processing report written to %s", path)
    return counts


def main() -> int:
    """Run safe, incremental Trusted publishing using a verified quality report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality-report", type=Path, default=Path("reports/audio_trusted_quality_full.csv"))
    parser.add_argument("--report", type=Path, default=Path("reports/audio_trusted_processing.csv"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    configure_logging()
    try:
        counts = run_trusted(create_minio_client(), load_storage_config(), args.quality_report,
                             limit=args.limit, dry_run=args.dry_run, report=args.report)
    except (ValueError, OSError, RuntimeError, ClientError, BotoCoreError) as exc:
        LOGGER.error("Trusted publication cannot start: %s", exc)
        return 1
    LOGGER.info("Summary: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 2 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

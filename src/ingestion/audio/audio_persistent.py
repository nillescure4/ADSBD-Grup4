"""Safely promote original audio objects from Temporal to Persistent Landing.

The source audio bytes are never transformed, and no collection manifest is read.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.common.config import load_storage_config
from src.common.logging import configure_logging
from src.common.minio_client import create_minio_client, list_object_keys

LOGGER = logging.getLogger(__name__)
AUDIO_PREFIX = "audio"
SOURCE_NAME = "opengameart"
DATASET_NAME = "music"
PERSISTENT_NAME = re.compile(
    rf"^{SOURCE_NAME}\${DATASET_NAME}\$(\d{{2}}-\d{{2}}-\d{{4}})\$(\d+)\$(.+)$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class PromotionStats:
    """Record the outcome of a batch promotion."""

    found: int = 0
    selected: int = 0
    planned: int = 0
    promoted: int = 0
    recovered: int = 0
    failed: int = 0


def audio_identity(relative_key: str) -> tuple[str, str]:
    """Extract the submission ID and original filename from an audio key."""
    parts = relative_key.split("/")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1]:
        raise ValueError(f"Expected <numeric_submission>/<filename>: {relative_key}")
    if parts[1] in {".", ".."}:
        raise ValueError(f"Invalid audio filename: {relative_key}")
    return parts[0], parts[1]


def persistent_object_key(
    temporal_key: str, temporal_prefix: str, persistent_prefix: str, uploaded_at: datetime
) -> str:
    """Build a deterministic destination from the initial Temporal upload date."""
    source_prefix = f"{temporal_prefix.strip('/')}/{AUDIO_PREFIX}/"
    if not temporal_key.startswith(source_prefix):
        raise ValueError(f"Unexpected Temporal key: {temporal_key}")
    submission, filename = audio_identity(temporal_key[len(source_prefix):])
    if uploaded_at.tzinfo is None:
        raise ValueError("An aware object upload timestamp is required")
    date = uploaded_at.astimezone(timezone.utc).strftime("%d-%m-%Y")
    return (
        f"{persistent_prefix.strip('/')}/{AUDIO_PREFIX}/"
        f"{SOURCE_NAME}${DATASET_NAME}${date}${submission}${filename}"
    )


def persistent_identity(object_key: str, persistent_prefix: str) -> tuple[str, str] | None:
    """Read an identity only from filenames matching our persistent convention."""
    prefix = f"{persistent_prefix.strip('/')}/{AUDIO_PREFIX}/"
    if not object_key.startswith(prefix):
        return None
    filename = object_key[len(prefix):]
    if "/" in filename:
        return None
    match = PERSISTENT_NAME.fullmatch(filename)
    if match is None:
        return None
    try:
        datetime.strptime(match.group(1), "%d-%m-%Y")
    except ValueError:
        return None
    return match.group(2), match.group(3)


def persistent_index(client: Any, bucket: str, persistent_prefix: str) -> dict[tuple[str, str], list[str]]:
    """Index existing Persistent files so reruns do not depend on the current date."""
    prefix = f"{persistent_prefix.strip('/')}/{AUDIO_PREFIX}/"
    result: dict[tuple[str, str], list[str]] = {}
    for key in list_object_keys(client, bucket, prefix):
        identity = persistent_identity(key, persistent_prefix)
        if identity is not None:
            result.setdefault(identity, []).append(key)
    return result


def remote_sha256(client: Any, bucket: str, key: str) -> str:
    """Stream the stored object to verify its actual bytes, not just its metadata."""
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    digest = hashlib.sha256()
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            if chunk:
                digest.update(chunk)
    finally:
        body.close()
        if hasattr(body, "release_conn"):
            body.release_conn()
    return digest.hexdigest()


def verified_object(client: Any, bucket: str, key: str, size: int, sha256: str) -> bool:
    """Check object size, SHA256 metadata, and SHA256 of its actual contents."""
    head = client.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != sha256:
        return False
    return remote_sha256(client, bucket, key) == sha256


def promote_audio(
    client: Any,
    *,
    bucket: str,
    temporal_prefix: str,
    persistent_prefix: str,
    limit: int | None = 5,
    dry_run: bool = False,
) -> PromotionStats:
    """Promote Temporal audio after verifying that Persistent holds the exact bytes."""
    temporal_root = f"{temporal_prefix.strip('/')}/{AUDIO_PREFIX}/"
    temporary_keys = sorted(list_object_keys(client, bucket, temporal_root))
    selected = temporary_keys if limit is None else temporary_keys[:limit]
    stats = PromotionStats(found=len(temporary_keys), selected=len(selected))
    existing = persistent_index(client, bucket, persistent_prefix)

    LOGGER.info("Found %s Temporal objects; selected %s", stats.found, stats.selected)
    for position, source_key in enumerate(selected, 1):
        try:
            identity = audio_identity(source_key[len(temporal_root):])
            source = client.head_object(Bucket=bucket, Key=source_key)
            size = source["ContentLength"]
            sha256 = source.get("Metadata", {}).get("sha256", "")
            if size <= 0 or not SHA256_PATTERN.fullmatch(sha256):
                raise ValueError("Source has no valid size and SHA256 metadata")
            destination = persistent_object_key(
                source_key, temporal_prefix, persistent_prefix, source["LastModified"]
            )
            known = existing.get(identity, [])
            if len(known) > 1:
                raise ValueError(f"Multiple persistent objects for {identity}")
            if known and known[0] != destination:
                # An earlier successful promotion takes precedence over a new date.
                destination = known[0]

            if dry_run:
                LOGGER.info("[%s/%s] [DRY RUN] %s -> %s", position, len(selected), source_key, destination)
                stats.planned += 1
                continue

            if known:
                if not verified_object(client, bucket, destination, size, sha256):
                    raise ValueError("Existing Persistent object differs from the source")
                # The source can be deleted only after the destination is verified.
                client.delete_object(Bucket=bucket, Key=source_key)
                stats.recovered += 1
                LOGGER.info("[%s/%s] RECOVERED: %s", position, len(selected), destination)
                continue

            # Use the shared client, but do not call the existing generic move helper:
            # it deletes Temporal before verifying the copied object.
            client.copy_object(
                Bucket=bucket,
                Key=destination,
                CopySource={"Bucket": bucket, "Key": source_key},
            )
            if not verified_object(client, bucket, destination, size, sha256):
                raise ValueError("Copy verification failed; Temporal has NOT been deleted")
            client.delete_object(Bucket=bucket, Key=source_key)
            existing[identity] = [destination]
            stats.promoted += 1
            LOGGER.info("[%s/%s] PROMOTED: %s", position, len(selected), destination)
        except Exception as exc:
            stats.failed += 1
            LOGGER.error("[%s/%s] FAILED: %s (%s)", position, len(selected), source_key, exc)
    return stats


def main() -> int:
    """Run a small, safe promotion batch unless --all is explicitly requested."""
    parser = argparse.ArgumentParser(description="Promote audio from Temporal to Persistent Landing")
    parser.add_argument("--dry-run", action="store_true", help="Preview destinations without modifying MinIO")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="Maximum objects (default: 5)")
    selection.add_argument("--all", action="store_true", help="Promote every Temporal audio object")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")

    configure_logging()
    storage = load_storage_config()
    try:
        stats = promote_audio(
            create_minio_client(),
            bucket=storage["buckets"]["landing"],
            temporal_prefix=storage["landing"]["temporal_prefix"],
            persistent_prefix=storage["landing"]["persistent_prefix"],
            limit=None if args.all else (args.limit if args.limit is not None else 5),
            dry_run=args.dry_run,
        )
    except Exception as exc:
        LOGGER.error("Promotion could not start: %s", exc)
        return 1
    LOGGER.info(
        "SUMMARY | found=%s selected=%s planned=%s promoted=%s recovered=%s failed=%s",
        stats.found, stats.selected, stats.planned, stats.promoted, stats.recovered, stats.failed,
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

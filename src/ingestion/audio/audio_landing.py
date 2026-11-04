"""Ingest original audio files into MinIO Temporal Landing.

This module deliberately does not read manifests or transform audio data.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.common.config import load_storage_config
from src.common.logging import configure_logging
from src.common.minio_client import create_minio_client, list_object_keys
from src.ingestion.audio.audio_persistent import audio_identity, persistent_index

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_AUDIO_DIR = PROJECT_ROOT / "data" / "landing" / "opengameart_audio" / "raw" / "files"
# Keep the collector, Landing and Formatted playable-audio contracts aligned.
AUDIO_EXTENSIONS = frozenset({".aac", ".aif", ".aiff", ".au", ".flac", ".m4a",
                              ".mp3", ".oga", ".ogg", ".opus", ".wav", ".wma"})
AUDIO_PREFIX = "audio"


@dataclass
class IngestionStats:
    """Track the result of one ingestion run."""

    found: int = 0
    selected: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    planned: int = 0


def discover_audio_files(audio_dir: Path) -> list[Path]:
    """Find supported audio files recursively, without following symlinked files."""
    if not audio_dir.is_dir():
        raise NotADirectoryError(f"Audio directory does not exist: {audio_dir}")

    return sorted(
        (
            path
            for path in audio_dir.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in AUDIO_EXTENSIONS
        ),
        key=lambda path: path.relative_to(audio_dir).as_posix(),
    )


def temporal_object_key(audio_dir: Path, path: Path, temporal_prefix: str) -> str:
    """Build a stable S3 key while preserving the original relative file path."""
    relative_path = path.relative_to(audio_dir).as_posix()
    return f"{temporal_prefix.strip('/')}/{AUDIO_PREFIX}/{relative_path}"


def sha256_file(path: Path) -> str:
    """Hash a file in chunks so large audio files never fill memory."""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_audio(
    audio_dir: Path,
    *,
    client: Any | None,
    bucket: str,
    temporal_prefix: str,
    persistent_prefix: str | None = None,
    limit: int | None = 5,
    dry_run: bool = False,
) -> IngestionStats:
    """Upload original audio files safely, without overwriting existing objects.

    SHA256 is recorded as S3 object metadata to support efficient repeat runs.
    If an existing key has different or unknown metadata, the file is rejected
    instead of being silently overwritten.
    """
    audio_dir = audio_dir.resolve()
    files = discover_audio_files(audio_dir)
    selected_files = files if limit is None else files[:limit]
    stats = IngestionStats(found=len(files), selected=len(selected_files))

    object_prefix = f"{temporal_prefix.strip('/')}/{AUDIO_PREFIX}/"
    LOGGER.info("Discovered %s audio files; selected %s", stats.found, stats.selected)

    if dry_run:
        for path in selected_files:
            key = temporal_object_key(audio_dir, path, temporal_prefix)
            LOGGER.info("[DRY RUN] %s -> s3://%s/%s", path, bucket, key)
            stats.planned += 1
        return stats

    if client is None:
        raise ValueError("An S3 client is required when dry_run is False")

    # Load existing keys once, avoiding a bucket-wide listing per file.
    existing_keys = set(list_object_keys(client, bucket, object_prefix))
    # Only consult Persistent when its configured prefix is provided.
    promoted_objects = (
        persistent_index(client, bucket, persistent_prefix)
        if persistent_prefix is not None else {}
    )

    for index, path in enumerate(selected_files, start=1):
        key = temporal_object_key(audio_dir, path, temporal_prefix)
        try:
            local_size = path.stat().st_size
            if local_size == 0:
                raise ValueError("Empty audio file")
            local_hash = sha256_file(path)

            # Persistent takes priority: promoted objects must never be uploaded again.
            identity = audio_identity(path.relative_to(audio_dir).as_posix())
            completed = promoted_objects.get(identity, [])
            if len(completed) > 1:
                raise ValueError("Multiple Persistent objects exist for this audio")
            if completed:
                remote = client.head_object(Bucket=bucket, Key=completed[0])
                if (
                    remote["ContentLength"] == local_size
                    and remote.get("Metadata", {}).get("sha256") == local_hash
                ):
                    stats.skipped += 1
                    LOGGER.info("[%s/%s] SKIP (already persistent): %s", index, stats.selected, completed[0])
                    continue
                raise ValueError("Persistent object differs from the local audio")

            if key in existing_keys:
                remote = client.head_object(Bucket=bucket, Key=key)
                remote_size = remote["ContentLength"]
                remote_hash = remote.get("Metadata", {}).get("sha256")
                if remote_size == local_size and remote_hash == local_hash:
                    stats.skipped += 1
                    LOGGER.info("[%s/%s] SKIP (already uploaded): %s", index, stats.selected, key)
                    continue
                raise ValueError(
                    "Existing object differs or has no SHA256 metadata; "
                    "refusing to overwrite it"
                )

            # The shared upload_file helper does not accept S3 metadata yet.
            # Use the shared S3 client directly to attach a SHA256 digest.
            client.upload_file(
                str(path),
                bucket,
                key,
                ExtraArgs={"Metadata": {"sha256": local_hash}},
            )
            remote = client.head_object(Bucket=bucket, Key=key)
            if (
                remote["ContentLength"] != local_size
                or remote.get("Metadata", {}).get("sha256") != local_hash
            ):
                raise RuntimeError("Uploaded object failed the size/metadata check")

            stats.uploaded += 1
            existing_keys.add(key)
            LOGGER.info("[%s/%s] UPLOADED: %s", index, stats.selected, key)
        except Exception as error:
            stats.failed += 1
            LOGGER.error("[%s/%s] FAILED: %s (%s)", index, stats.selected, key, error)

    return stats


def main() -> int:
    """Run a small, safe ingestion batch unless --all is explicitly requested."""
    parser = argparse.ArgumentParser(description="Ingest original audio into Temporal Landing")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_AUDIO_DIR,
        help="Collector audio folder (default: data/landing/opengameart_audio/raw/files)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show planned keys without using MinIO")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="Maximum files (default: 5)")
    selection.add_argument("--all", action="store_true", help="Select every supported audio file")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")

    configure_logging()
    storage = load_storage_config()
    bucket = storage["buckets"]["landing"]
    temporal_prefix = storage["landing"]["temporal_prefix"]
    persistent_prefix = storage["landing"]["persistent_prefix"]
    limit = None if args.all else (args.limit if args.limit is not None else 5)

    try:
        # Dry runs intentionally do not connect to MinIO.
        client = None if args.dry_run else create_minio_client()
        stats = ingest_audio(
            args.input_dir,
            client=client,
            bucket=bucket,
            temporal_prefix=temporal_prefix,
            persistent_prefix=persistent_prefix,
            limit=limit,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError) as error:
        LOGGER.error("Ingestion could not start: %s", error)
        return 1

    LOGGER.info(
        "SUMMARY | found=%s selected=%s planned=%s uploaded=%s skipped=%s failed=%s",
        stats.found,
        stats.selected,
        stats.planned,
        stats.uploaded,
        stats.skipped,
        stats.failed,
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

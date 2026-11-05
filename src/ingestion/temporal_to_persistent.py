"""Promote a validated object without overwriting different Persistent content."""

from __future__ import annotations

import argparse

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client
from src.ingestion.images.images_temporal_to_persistent import move_object


def promote_object(source_key: str, destination_key: str) -> None:
    """Copy, verify contents, then remove Temporal; identical retries are safe."""
    storage = load_storage_config()
    bucket = storage["buckets"]["landing"]
    temporal = storage["landing"]["temporal_prefix"].rstrip("/") + "/"
    persistent = storage["landing"]["persistent_prefix"].rstrip("/") + "/"

    if not source_key.startswith(temporal):
        raise ValueError(f"The source key must start with {temporal}")
    if not destination_key.startswith(persistent):
        raise ValueError(f"The destination key must start with {persistent}")

    client = create_minio_client()
    move_object(client, bucket, source_key, destination_key)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__
    )
    parser.add_argument("source_key", help="Example: temporal/source/dataset/file.json")
    parser.add_argument(
        "destination_key",
        help="Example: persistent/source/dataset/2026/10/source$dataset$13-10-2026.json",
    )
    args = parser.parse_args()
    promote_object(args.source_key, args.destination_key)


if __name__ == "__main__":
    main()


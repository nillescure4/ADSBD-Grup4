#!/usr/bin/env python3
"""Move images from Landing temporal to persistent, renaming them to the standard name."""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import timezone
from pathlib import PurePosixPath

from botocore.exceptions import ClientError

from src.common.config import load_yaml, load_storage_config
from src.common.minio_client import create_minio_client

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def build_standard_name(source: str, dataset_name: str, key: str, last_modified) -> str:
    """Build source$dataset$name$dd-mm-yyyy.format."""
    path = PurePosixPath(key)
    date = last_modified.astimezone(timezone.utc).strftime("%d-%m-%Y")
    return f"{source}${dataset_name}${path.stem}${date}{path.suffix.lower()}"


def head_or_none(client, bucket: str, key: str) -> dict | None:
    """Object metadata, or None if the key does not exist."""
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise


def same_object(a: dict, b: dict) -> bool:
    return a["ContentLength"] == b["ContentLength"] and a["ETag"] == b["ETag"]


def same_content(client, bucket, source_key, destination, source_meta, destination_meta):
    """Multipart uploads can have different ETags after a byte-identical copy."""
    if same_object(source_meta, destination_meta):
        return True
    if source_meta["ContentLength"] != destination_meta["ContentLength"]:
        return False
    def digest(key):
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        result = hashlib.sha256()
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                result.update(chunk)
        finally:
            body.close()
        return result.digest()
    return digest(source_key) == digest(destination)


def move_object(client, bucket: str, source_key: str, destination: str) -> None:
    """Copy to destination, verify, then delete the source. Never overwrites different content."""
    source_meta = client.head_object(Bucket=bucket, Key=source_key)
    existing = head_or_none(client, bucket, destination)

    if existing is not None:
        if not same_content(client, bucket, source_key, destination, source_meta, existing):
            raise ValueError("destination already exists with different content")
    else:
        client.copy_object(
            Bucket=bucket,
            Key=destination,
            CopySource={"Bucket": bucket, "Key": source_key},
        )
        if not same_content(client, bucket, source_key, destination, source_meta,
                            client.head_object(Bucket=bucket, Key=destination)):
            raise ValueError("copy does not match the original")

    client.delete_object(Bucket=bucket, Key=source_key)


def move_dataset_to_persistent(client, bucket, temporal_root, persistent_root,
                               name, dataset, dry_run) -> tuple[int, int]:
    """Rename temporal/images/<dataset>/* and move it to persistent/images/. Returns (moved, errors)."""
    source = dataset["source"]
    prefix = f"{temporal_root}/images/{name}/"
    paginator = client.get_paginator("list_objects_v2")
    moved, errors = 0, 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if PurePosixPath(key).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            destination = f"{persistent_root}/images/" + build_standard_name(
                source, name, key, obj["LastModified"])
            print(f"{key} -> {destination}")
            if dry_run:
                continue
            try:
                move_object(client, bucket, key, destination)
                moved += 1
            except Exception as exc:
                errors += 1
                print(f"  [ERROR] {key}: {exc}", file=sys.stderr)

    return moved, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the destinations without modifying MinIO.")
    args = parser.parse_args()

    storage = load_storage_config()
    bucket = storage["buckets"]["landing"]
    temporal_root = storage["landing"]["temporal_prefix"].strip("/")
    persistent_root = storage["landing"]["persistent_prefix"].strip("/")
    datasets = load_yaml("datasets.yml")["datasets"]

    client = create_minio_client()
    client.head_bucket(Bucket=bucket)

    total_moved, total_errors = 0, 0
    for name, dataset in datasets.items():
        if dataset.get("modality") != "image":
            continue
        print(f"Moving dataset '{name}' to persistent...")
        moved, errors = move_dataset_to_persistent(
            client, bucket, temporal_root, persistent_root, name, dataset, args.dry_run)
        total_moved += moved
        total_errors += errors
        print(f"[OK] {name}: {moved} moved, {errors} errors")

    if total_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

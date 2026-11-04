"""Promote original RAWG text captures from Temporal to Persistent Landing."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

from botocore.exceptions import ClientError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client, list_object_keys


def read_bytes(client: Any, bucket: str, key: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    try:
        return response["Body"].read()
    finally:
        response["Body"].close()


def read_optional(client: Any, bucket: str, key: str) -> bytes | None:
    try:
        return read_bytes(client, bucket, key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise


def promote_texts(*, dry_run: bool = False) -> int:
    storage = load_storage_config()
    bucket = storage["buckets"]["landing"]
    landing = storage["landing"]
    temporal = f"{landing['temporal_prefix'].strip('/')}/{landing['texts_prefix'].strip('/')}/"
    persistent = f"{landing['persistent_prefix'].strip('/')}/{landing['texts_prefix'].strip('/')}/rawg/"
    client = create_minio_client()
    # Only direct game JSONs are data; the JSONL manifest is handled separately.
    keys = sorted(
        key for key in list_object_keys(client, bucket, temporal)
        if "/" not in key[len(temporal):]
        and Path(key).suffix == ".json"
        and Path(key).stem.isdigit()
    )
    if not keys:
        print("No game JSON files are pending in Temporal Landing.")
        return 0

    manifest_key = f"{temporal}manifests/text_sources.jsonl"
    raw_manifest = read_bytes(client, bucket, manifest_key).decode("utf-8")
    records: dict[int, dict[str, Any]] = {}
    for line in raw_manifest.splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("source") == "rawg":
                records[int(row["source_game_id"])] = row

    errors = 0
    promoted = 0
    for source_key in keys:
        try:
            game_id = int(Path(source_key).stem)
            row = records[game_id]
            collected = datetime.fromisoformat(row["ingested_at_utc"])
            if collected.tzinfo is None:
                raise ValueError("The capture timestamp must include a timezone")
            date = collected.astimezone(timezone.utc).strftime("%d-%m-%Y")
            destination = f"{persistent}rawg$game_details_{game_id}${date}.json"
            print(f"{source_key} -> {destination}")
            if dry_run:
                continue

            original = read_bytes(client, bucket, source_key)
            digest = hashlib.sha256(original).hexdigest()
            if row.get("sha256") and row["sha256"] != digest:
                raise ValueError("The Temporal JSON does not match its collection manifest")
            existing = read_optional(client, bucket, destination)
            if existing is not None and existing != original:
                raise ValueError("The destination already exists with different content")
            if existing is None:
                client.copy_object(
                    Bucket=bucket,
                    Key=destination,
                    CopySource={"Bucket": bucket, "Key": source_key},
                )
            copied = read_bytes(client, bucket, destination)
            if hashlib.sha256(copied).hexdigest() != digest:
                raise ValueError("The copy does not match the original")

            # One provenance record per game/date avoids overwriting past batches.
            record_key = f"{persistent}manifests/rawg$text_sources_{game_id}${date}.jsonl"
            record = {
                **row,
                "temporal_object_key": source_key,
                "persistent_object_key": destination,
                "bucket": bucket,
                "sha256": digest,
            }
            record_bytes = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            client.put_object(
                Bucket=bucket, Key=record_key, Body=record_bytes,
                ContentType="application/x-ndjson",
            )
            if read_bytes(client, bucket, record_key) != record_bytes:
                raise ValueError("Could not verify the Persistent manifest")
            client.delete_object(Bucket=bucket, Key=source_key)
            promoted += 1
        except Exception as exc:
            errors += 1
            print(f"[ERROR] {source_key}: {exc}", file=sys.stderr)

    print(f"{'Dry run' if dry_run else 'Promotion'}: {len(keys)} candidates, {promoted} moved, {errors} errors.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show destinations without modifying MinIO.")
    args = parser.parse_args()
    raise SystemExit(promote_texts(dry_run=args.dry_run))


if __name__ == "__main__":
    main()

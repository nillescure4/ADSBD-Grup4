"""Publish explicit RAWG game associations for verified Trusted screenshots."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath

from botocore.exceptions import ClientError

from src.common.config import load_storage_config, load_yaml
from src.common.minio_client import create_minio_client, list_object_keys
from src.formatting.images.persistent_to_formatted import convert_to_jpeg, formatted_image_key


def read_bytes(client, bucket, key):
    response = client.get_object(Bucket=bucket, Key=key)
    try:
        return response["Body"].read()
    finally:
        response["Body"].close()


def publish(client, storage, root, dataset):
    """Join the collector manifest to original bytes and canonical Trusted copies.

    Filename matching only locates candidate objects. A raw SHA-256 and a
    reproduced JPEG digest establish the association; game IDs are never guessed.
    Existing, still-current associations from earlier runs are retained.
    """
    source = load_yaml("datasets.yml")["datasets"][dataset]["source"]
    if source != "rawg":
        raise ValueError("Image provenance requires a RAWG dataset")
    landing, formatted, trusted = (storage["buckets"][zone] for zone in ("landing", "formatted", "trusted"))
    prefix = storage["landing"]["persistent_prefix"].strip("/") + "/images/"
    key = storage.get("trusted", {}).get("images_provenance_key", "provenance/images.jsonl")
    current = {obj["Key"]: obj["ETag"].strip('"')
               for page in client.get_paginator("list_objects_v2").paginate(Bucket=trusted, Prefix="images/")
               for obj in page.get("Contents", [])}
    try:
        old_body = read_bytes(client, trusted, key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"NoSuchKey", "404", "NotFound"}:
            raise
        old_body = b""
    records = {row["object_key"]: row for row in
               (json.loads(line) for line in old_body.splitlines() if line.strip())
               if current.get(row["object_key"]) == row["etag"]}
    manifest = root / "manifests/images.jsonl"
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("The screenshot provenance manifest is empty")
    by_name = {}
    for row in rows:
        if type(row.get("source_game_id")) is not int or row["source_game_id"] <= 0:
            raise ValueError("Invalid game identifier in the image manifest")
        name = PurePosixPath(row["local_path"].replace("\\", "/")).name
        previous = by_name.get(name)
        if previous and (previous["source_game_id"], previous["sha256"]) != (row["source_game_id"], row["sha256"]):
            raise ValueError(f"Ambiguous image manifest filename: {name}")
        by_name[name] = row
    for raw_key in list_object_keys(client, landing, prefix):
        parts = PurePosixPath(raw_key).name.split("$")
        if len(parts) != 4 or parts[:2] != [source, dataset]:
            continue
        row = by_name.get(parts[2] + PurePosixPath(raw_key).suffix)
        target = formatted_image_key(raw_key)
        if row is None or target not in current:
            continue
        raw = read_bytes(client, landing, raw_key)
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError(f"Original screenshot does not match its manifest: {raw_key}")
        canonical = convert_to_jpeg(raw)
        digest = hashlib.sha256(canonical).hexdigest()
        if (read_bytes(client, formatted, target) != canonical
                or hashlib.sha256(read_bytes(client, trusted, target)).hexdigest() != digest):
            raise ValueError(f"Screenshot lineage could not be verified: {target}")
        record = {"object_key": target, "etag": current[target], "game_id": row["source_game_id"],
                  "game_name": row.get("game_name") or row.get("name") or "",
                  "source_url": row["source_url"], "sha256": digest, "raw_sha256": row["sha256"],
                  "landing_object_key": raw_key}
        previous = records.get(target)
        if previous and previous["game_id"] != record["game_id"]:
            raise ValueError(f"Conflicting game association: {target}")
        records[target] = record
    body = b"".join((json.dumps(records[name], sort_keys=True) + "\n").encode("utf-8") for name in sorted(records))
    if body != old_body:
        client.put_object(Bucket=trusted, Key=key, Body=body, ContentType="application/x-ndjson")
        if read_bytes(client, trusted, key) != body:
            raise ValueError("Image provenance publication could not be verified")
    return len(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    args = parser.parse_args()
    count = publish(create_minio_client(), load_storage_config(), args.input_dir, args.dataset)
    print(f"Verified image/game associations: {count}")


if __name__ == "__main__":
    main()

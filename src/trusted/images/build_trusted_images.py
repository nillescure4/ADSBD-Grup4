#!/usr/bin/env python3
"""Build the Trusted image zone: measure Formatted images, apply quality rules, copy the accepted ones."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import pandas as pd
from botocore.exceptions import ClientError

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client
from src.trusted.images import rules
from src.trusted.images.metrics import compute_metrics

IMAGES_PREFIX = "images/"            # same prefix in Formatted and Trusted
QUALITY_PREFIX = "quality/images/"   # reports and checkpoint, in the Trusted bucket


def list_objects(client, bucket: str, prefix: str) -> dict[str, str]:
    """key -> ETag for every object under prefix."""
    found = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            found[obj["Key"]] = obj["ETag"]
    return found


def read_json_or_none(client, bucket: str, key: str) -> dict | None:
    try:
        return json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise


def put_verified(client, bucket: str, key: str, body: bytes, content_type: str) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    if client.head_object(Bucket=bucket, Key=key)["ContentLength"] != len(body):
        raise ValueError(f"could not verify {key}")


def input_signature(formatted: dict[str, str]) -> dict:
    """What the decision depends on: the rules, their thresholds and the exact set of input images."""
    listing = json.dumps(sorted(formatted.items())).encode("utf-8")
    return {
        "rules_version": rules.RULES_VERSION,
        "thresholds": dict(rules.THRESHOLDS),
        "images": len(formatted),
        "listing_sha256": hashlib.sha256(listing).hexdigest(),
    }


def collect_metrics(client, bucket: str, keys: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Measure every image. Returns the metrics table and the keys that could not be read.

    An image that cannot be decoded is a quality problem and ends up as 'corrupt'. An image that
    cannot be read from MinIO is an infrastructure problem: it must never be recorded as corrupt,
    because the decision would be checkpointed and the image would stay out of Trusted.
    """
    rows, unreadable = [], []
    for i, key in enumerate(keys, start=1):
        try:
            raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:
            unreadable.append(key)
            print(f"[ERROR] {key}: could not be read ({type(exc).__name__})", file=sys.stderr)
            continue
        row = {"key": key, "error": None}
        try:
            row.update(compute_metrics(raw))
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
        if i % 500 == 0:
            print(f"  {i}/{len(keys)}")
    return pd.DataFrame(rows), unreadable


def sync_trusted(client, src_bucket: str, dst_bucket: str, formatted: dict[str, str],
                 accepted: list[str], *, dry_run: bool, prune: bool) -> int:
    """Make Trusted images/ contain exactly the accepted images. Returns the error count."""
    existing = list_objects(client, dst_bucket, IMAGES_PREFIX)
    wanted = {f"{IMAGES_PREFIX}{PurePosixPath(key).name}": key for key in accepted}
    copied = skipped = removed = errors = 0

    for dest, key in sorted(wanted.items()):
        if existing.get(dest) == formatted[key]:
            skipped += 1
            continue
        if dry_run:
            print(f"{key} -> {dest}")
            continue
        try:
            client.copy_object(Bucket=dst_bucket, Key=dest,
                               CopySource={"Bucket": src_bucket, "Key": key})
            if client.head_object(Bucket=dst_bucket, Key=dest)["ETag"] != formatted[key]:
                raise ValueError("copy does not match the original")
            copied += 1
        except Exception as exc:
            errors += 1
            print(f"[ERROR] {key}: {exc}", file=sys.stderr)

    stale = sorted(set(existing) - set(wanted))
    if stale:
        if prune and not dry_run:
            for dest in stale:
                try:
                    client.delete_object(Bucket=dst_bucket, Key=dest)
                    removed += 1
                except Exception as exc:
                    errors += 1
                    print(f"[ERROR] delete {dest}: {exc}", file=sys.stderr)
        else:
            print(f"[WARN] {len(stale)} images in Trusted are no longer accepted "
                  f"(use --prune to delete them)")

    print(f"Trusted: {copied} copied, {skipped} up to date, {removed} pruned, {errors} errors.")
    return errors


def build_trusted_images(*, dry_run: bool, prune: bool, csv_output: Path) -> int:
    storage = load_storage_config()
    src_bucket, dst_bucket = storage["buckets"]["formatted"], storage["buckets"]["trusted"]
    client = create_minio_client()
    client.head_bucket(Bucket=dst_bucket)

    formatted = list_objects(client, src_bucket, IMAGES_PREFIX)
    if not formatted:
        print("No images in Formatted.")
        return 0

    signature = input_signature(formatted)
    checkpoint_key = f"{QUALITY_PREFIX}checkpoint.json"
    checkpoint = read_json_or_none(client, dst_bucket, checkpoint_key)

    if checkpoint and checkpoint["input"] == signature:
        print("[SKIP] Inputs and rules unchanged; verifying Trusted outputs only")
        if not dry_run:
            # Restore the report for downstream analysis even in a new local directory.
            response = client.get_object(Bucket=dst_bucket, Key=f"{QUALITY_PREFIX}images_quality.csv")
            try:
                report_bytes = response["Body"].read()
            finally:
                response["Body"].close()
            csv_output.parent.mkdir(parents=True, exist_ok=True)
            csv_output.write_bytes(report_bytes)
        errors = sync_trusted(client, src_bucket, dst_bucket, formatted,
                              checkpoint["accepted"], dry_run=dry_run, prune=prune)
        return 1 if errors else 0

    print(f"Measuring {len(formatted)} images...")
    metrics, unreadable = collect_metrics(client, src_bucket, sorted(formatted))
    if unreadable:
        print(f"[ERROR] {len(unreadable)} images could not be read from MinIO; nothing was written. "
              f"Fix the connection and run again.", file=sys.stderr)
        return 1
    df = rules.apply_rules(rules.flag_duplicates(metrics))
    accepted = df.loc[df["reject_reason"].isna(), "key"].tolist()
    outcomes = df["reject_reason"].fillna("accepted").value_counts()

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rules_version": rules.RULES_VERSION,
        "thresholds": rules.THRESHOLDS,
        "images_seen": len(df),
        "accepted": len(accepted),
        "outcomes": {name: int(count) for name, count in outcomes.items()},
        "flag_counts": {c: int(df[c].sum()) for c in df.columns if c.startswith(("flag_", "dup_"))},
    }
    print(json.dumps(summary, indent=2))

    if dry_run:
        sync_trusted(client, src_bucket, dst_bucket, formatted, accepted, dry_run=True, prune=prune)
        return 0

    # Bytes with "\n" endings on every platform: write_text() on Windows would turn "\r\n" into "\r\r\n"
    csv_bytes = df.to_csv(index=False).replace("\r\n", "\n").encode("utf-8")
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.write_bytes(csv_bytes)
    put_verified(client, dst_bucket, f"{QUALITY_PREFIX}images_quality.csv", csv_bytes, "text/csv")
    put_verified(client, dst_bucket, f"{QUALITY_PREFIX}summary.json",
                 json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
                 "application/json")

    errors = sync_trusted(client, src_bucket, dst_bucket, formatted, accepted,
                          dry_run=False, prune=prune)
    if errors:
        print("[WARN] No checkpoint written; the next run will retry.", file=sys.stderr)
        return 1

    body = json.dumps({"input": signature, "accepted": accepted, "summary": summary},
                      ensure_ascii=False).encode("utf-8")
    put_verified(client, dst_bucket, checkpoint_key, body, "application/json")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv_output", default="reports/images/images_quality.csv",
                        help="Local path for the quality CSV (also uploaded to Trusted).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Measure and report without writing to MinIO or disk.")
    parser.add_argument("--prune", action="store_true",
                        help="Delete Trusted images that are no longer accepted.")
    args = parser.parse_args()
    raise SystemExit(build_trusted_images(
        dry_run=args.dry_run, prune=args.prune, csv_output=Path(args.csv_output)))


if __name__ == "__main__":
    main()

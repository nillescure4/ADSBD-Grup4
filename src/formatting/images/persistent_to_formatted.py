#!/usr/bin/env python3
"""Convert persistent Landing images to JPEG in the Formatted zone."""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import PurePosixPath

from botocore.exceptions import ClientError
from PIL import Image, ImageOps, UnidentifiedImageError

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client

DST_PREFIX = "images/"
QUARANTINE_PREFIX = "quarantine/images/"
JPEG_QUALITY = 95
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
BAD_IMAGE_ERRORS = (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError)


def to_rgb(img: Image.Image) -> Image.Image:
    """Convert any Pillow mode to RGB."""
    if getattr(img, "n_frames", 1) > 1:   # animated GIF/WEBP, multipage TIFF
        img.seek(0)

    img = ImageOps.exif_transpose(img)    # apply orientation before EXIF is lost

    if img.mode in ("I", "I;16", "I;16L", "I;16B", "F"):  # 16/32 bit -> 8 bit
        img = img.point(lambda v: v * (1 / 256)).convert("L")

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background

    return img.convert("RGB")


def convert_to_jpeg(raw: bytes) -> bytes:
    with Image.open(io.BytesIO(raw)) as img:
        img.load()  # raises if corrupt or truncated
        rgb = to_rgb(img)
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
        return buffer.getvalue()


def head_or_none(client, bucket: str, key: str) -> dict | None:
    """Object metadata, or None only if the key does not exist."""
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise


def format_images(*, dry_run: bool = False) -> int:
    storage = load_storage_config()
    source_bucket = storage["buckets"]["landing"]
    target_bucket = storage["buckets"]["formatted"]
    source_prefix = f"{storage['landing']['persistent_prefix'].strip('/')}/images/"

    client = create_minio_client()
    client.head_bucket(Bucket=target_bucket)

    converted = skipped = quarantined = errors = 0
    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=source_bucket, Prefix=source_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            path = PurePosixPath(key)
            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue

            target_key = f"{DST_PREFIX}{path.stem}.jpg"
            try:
                if head_or_none(client, target_bucket, target_key) is not None:
                    skipped += 1
                    continue

                raw = client.get_object(Bucket=source_bucket, Key=key)["Body"].read()
                try:
                    data = convert_to_jpeg(raw)
                except BAD_IMAGE_ERRORS as exc:
                    print(f"[QUARANTINE] {key}: {exc}", file=sys.stderr)
                    if not dry_run:
                        client.copy_object(
                            Bucket=target_bucket,
                            Key=f"{QUARANTINE_PREFIX}{path.name}",
                            CopySource={"Bucket": source_bucket, "Key": key},
                        )
                    quarantined += 1
                    continue

                print(f"{key} -> {target_key}")
                if dry_run:
                    continue

                client.put_object(
                    Bucket=target_bucket, Key=target_key,
                    Body=data, ContentType="image/jpeg",
                )
                if head_or_none(client, target_bucket, target_key)["ContentLength"] != len(data):
                    raise ValueError("uploaded size does not match the converted image")
                converted += 1
            except Exception as exc:
                errors += 1
                print(f"[ERROR] {key}: {exc}", file=sys.stderr)

    print(f"Formatted: {converted} converted, {skipped} skipped, "
          f"{quarantined} quarantined, {errors} errors.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate and list conversions without writing to MinIO.")
    args = parser.parse_args()
    raise SystemExit(format_images(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
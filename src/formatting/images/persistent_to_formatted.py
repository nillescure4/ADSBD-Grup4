# src/formatted/format_images.py
import io
from pathlib import PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError

from src.common.config import load_yaml
from src.common.minio_client import create_minio_client

SRC_PREFIX = "persistent/images/"
DST_PREFIX = "images/"
QUARANTINE_PREFIX = "quarantine/images/"
JPEG_QUALITY = 95


def to_rgb(img: Image.Image) -> Image.Image:
    """Converteix qualsevol mode de Pillow a RGB."""
    if getattr(img, "n_frames", 1) > 1:   # GIF/WEBP animats, TIFF multipàgina
        img.seek(0)

    img = ImageOps.exif_transpose(img)    # aplica l'orientació abans de perdre l'EXIF

    if img.mode in ("I", "I;16", "I;16L", "I;16B", "F"):  # 16/32 bits -> 8 bits
        img = img.point(lambda v: v * (1 / 256)).convert("L")

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background

    return img.convert("RGB")


def convert_to_jpeg(raw: bytes) -> bytes:
    with Image.open(io.BytesIO(raw)) as img:
        img.load()  # llença error si està corrupta o truncada
        rgb = to_rgb(img)
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
        return buffer.getvalue()


def key_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except client.exceptions.ClientError:
        return False


def main():
    client = create_minio_client()
    buckets = load_yaml("storage.yml")["buckets"]
    bucket_landing, bucket_formatted = buckets["landing"], buckets["formatted"]

    ok = skipped = failed = 0
    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket_landing, Prefix=SRC_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            print(f"Processant {key}...")
            new_key = f"{DST_PREFIX}{PurePosixPath(key).stem}.jpg"

            if key_exists(client, bucket_formatted, new_key):
                skipped += 1
                continue

            try:
                raw = client.get_object(Bucket=bucket_landing, Key=key)["Body"].read()
                data = convert_to_jpeg(raw)
            except (UnidentifiedImageError, OSError, ValueError) as e:
                print(f"[ERROR] {key}: {e}")
                client.copy(
                    CopySource={"Bucket": bucket_landing, "Key": key},
                    Bucket=bucket_formatted,
                    Key=f"{QUARANTINE_PREFIX}{PurePosixPath(key).name}",
                )
                failed += 1
                continue

            client.put_object(
                Bucket=bucket_formatted,
                Key=new_key,
                Body=data,
                ContentType="image/jpeg",
            )
            ok += 1

    print(f"Convertides: {ok} | Ometudes: {skipped} | Quarantena: {failed}")


if __name__ == "__main__":
    main()
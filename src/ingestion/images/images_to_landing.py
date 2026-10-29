#!/usr/bin/env python3
"""Upload local image datasets (a directory of subdirectories of images) to Landing temporal."""
from __future__ import annotations

import sys
from pathlib import Path

from src.common.config import PROJECT_ROOT, load_yaml, load_storage_config
from src.common.minio_client import create_minio_client, upload_file

REQUIRED_KEYS = ("modality", "directory", "source")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def iter_image_files(root: Path):
    """All images under root, flat or nested at any depth."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.startswith(".") \
                and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def load_images_to_landing(name: str, dataset: dict, client, storage: dict) -> int:
    """Upload a dataset's images to landing/<temporal>/<images>/<dataset>/. Returns the error count."""
    missing = [k for k in REQUIRED_KEYS if k not in dataset]
    if missing:
        raise ValueError(f"Dataset '{name}': missing keys {missing} in datasets.yml")
    if dataset["modality"] != "image":
        raise ValueError(f"Dataset '{name}' is not an image dataset.")

    images_path = PROJECT_ROOT / "data" / "landing" / dataset["directory"]
    if not images_path.is_dir():
        raise FileNotFoundError(f"Directory not found: {images_path}")

    bucket = storage["buckets"]["landing"]
    prefix = f"{storage['landing']['temporal_prefix']}/images/{name}"

    uploaded, errors = 0, 0
    seen_names: dict[str, Path] = {}

    for image_file in iter_image_files(images_path):
        # Subdirectories are flattened, so a repeated filename would silently overwrite another image
        if image_file.name in seen_names:
            errors += 1
            print(f"  [WARN] duplicate filename {image_file.name}: "
                  f"{image_file} vs {seen_names[image_file.name]}", file=sys.stderr)
            continue
        seen_names[image_file.name] = image_file

        try:
            upload_file(client, image_file, bucket, f"{prefix}/{image_file.name}")
            uploaded += 1
        except Exception as exc:
            errors += 1
            print(f"  [WARN] {image_file} failed ({type(exc).__name__})", file=sys.stderr)

    print(f"[OK] {name}: {uploaded} images uploaded to '{bucket}/{prefix}/' ({errors} errors)")
    return errors


def main() -> None:
    datasets = load_yaml("datasets.yml")["datasets"]
    image_datasets = {n: d for n, d in datasets.items() if d.get("modality") == "image"}
    if not image_datasets:
        raise SystemExit("No image dataset found in datasets.yml")

    storage = load_storage_config()
    client = create_minio_client()
    client.head_bucket(Bucket=storage["buckets"]["landing"])

    total_errors = 0
    for name, dataset in image_datasets.items():
        print(f"Processing dataset '{name}'...")
        try:
            total_errors += load_images_to_landing(name, dataset, client, storage)
        except Exception as exc:
            total_errors += 1
            print(f"[ERROR] {name}: {exc}", file=sys.stderr)

    if total_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
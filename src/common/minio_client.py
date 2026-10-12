"""Client S3 compartit per interactuar amb MinIO."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3

from src.common.config import load_minio_settings


def create_minio_client() -> Any:
    """Crea un client S3 configurat per al MinIO local."""
    return boto3.client("s3", **load_minio_settings())


def list_bucket_names(client: Any) -> list[str]:
    """Retorna els noms dels buckets disponibles."""
    response = client.list_buckets()
    return sorted(bucket["Name"] for bucket in response.get("Buckets", []))


def upload_file(client: Any, local_path: Path, bucket: str, object_key: str) -> None:
    """Puja un fitxer local a un bucket de MinIO."""
    client.upload_file(str(local_path), bucket, object_key)


def list_object_keys(client: Any, bucket: str, prefix: str = "") -> list[str]:
    """Llista tots els objectes d'un prefix, inclosa la paginacio."""
    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    return [item["Key"] for page in pages for item in page.get("Contents", [])]


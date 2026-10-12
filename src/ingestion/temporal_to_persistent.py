"""Promou objectes validats de Landing temporal a Landing persistent."""

from __future__ import annotations

import argparse

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client


def promote_object(source_key: str, destination_key: str) -> None:
    """Copia un objecte validat a persistent i elimina l'entrada temporal."""
    storage = load_storage_config()
    bucket = storage["buckets"]["landing"]
    temporal = storage["landing"]["temporal_prefix"].rstrip("/") + "/"
    persistent = storage["landing"]["persistent_prefix"].rstrip("/") + "/"

    if not source_key.startswith(temporal):
        raise ValueError(f"L'origen ha de comencar per {temporal}")
    if not destination_key.startswith(persistent):
        raise ValueError(f"La destinacio ha de comencar per {persistent}")

    client = create_minio_client()
    client.head_object(Bucket=bucket, Key=source_key)
    client.copy_object(
        Bucket=bucket,
        Key=destination_key,
        CopySource={"Bucket": bucket, "Key": source_key},
    )
    client.delete_object(Bucket=bucket, Key=source_key)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mou un objecte validat de temporal a persistent."
    )
    parser.add_argument("source_key", help="Ex.: temporal/source/dataset/file.json")
    parser.add_argument(
        "destination_key",
        help="Ex.: persistent/source/dataset/2026/10/source$dataset$13-10-2026.json",
    )
    args = parser.parse_args()
    promote_object(args.source_key, args.destination_key)


if __name__ == "__main__":
    main()


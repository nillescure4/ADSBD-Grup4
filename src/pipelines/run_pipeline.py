"""Punt d'entrada del pipeline i comprovacio de l'entorn local."""

from __future__ import annotations

import argparse

from src.common.config import load_storage_config
from src.common.logging import configure_logging
from src.common.minio_client import create_minio_client, list_bucket_names


def check_status() -> int:
    """Comprova la connexio i que existeixin tots els buckets configurats."""
    expected = set(load_storage_config()["buckets"].values())
    existing = set(list_bucket_names(create_minio_client()))

    print("Buckets configurats:")
    for bucket in sorted(expected):
        state = "OK" if bucket in existing else "FALTA"
        print(f"  [{state}] {bucket}")

    return 0 if expected <= existing else 1


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Pipeline de dades ADSBD Grup 4")
    parser.add_argument(
        "command",
        choices=["status"],
        help="Accio que s'ha d'executar.",
    )
    args = parser.parse_args()

    if args.command == "status":
        raise SystemExit(check_status())


if __name__ == "__main__":
    main()


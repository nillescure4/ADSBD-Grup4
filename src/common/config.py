"""Carrega la configuracio del projecte i les credencials locals."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(filename: str) -> dict[str, Any]:
    """Carrega un fitxer YAML de la carpeta config."""
    path = CONFIG_DIR / filename
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def load_storage_config() -> dict[str, Any]:
    """Retorna la configuracio dels buckets i prefixos."""
    return load_yaml("storage.yml")


def load_datasets_config() -> dict[str, Any]:
    """Retorna el cataleg de fonts i datasets."""
    return load_yaml("datasets.yml")


def load_minio_settings() -> dict[str, str]:
    """Retorna les dades de connexio a MinIO sense exposar secrets."""
    load_dotenv(PROJECT_ROOT / ".env")

    username = os.getenv("MINIO_ROOT_USER")
    password = os.getenv("MINIO_ROOT_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "Falten MINIO_ROOT_USER o MINIO_ROOT_PASSWORD al fitxer .env"
        )

    return {
        "endpoint_url": os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
        "aws_access_key_id": username,
        "aws_secret_access_key": password,
        "region_name": "us-east-1",
    }


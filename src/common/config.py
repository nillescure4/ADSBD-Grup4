"""Load project settings and local credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(filename: str) -> dict[str, Any]:
    """Load a YAML file from the config directory."""
    path = CONFIG_DIR / filename
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def load_storage_config() -> dict[str, Any]:
    """Load storage settings, optionally overridden for an isolated pipeline run."""
    override = os.getenv("ADSBD_STORAGE_CONFIG")
    if override:
        with Path(override).open(encoding="utf-8") as stream:
            return yaml.safe_load(stream) or {}
    return load_yaml("storage.yml")


def load_datasets_config() -> dict[str, Any]:
    """Return the source and dataset catalogue."""
    return load_yaml("datasets.yml")


def load_minio_settings() -> dict[str, str]:
    """Return MinIO connection settings without exposing secrets."""
    load_dotenv(PROJECT_ROOT / ".env")

    username = os.getenv("MINIO_ROOT_USER")
    password = os.getenv("MINIO_ROOT_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "MINIO_ROOT_USER or MINIO_ROOT_PASSWORD is missing from .env"
        )

    return {
        "endpoint_url": os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
        "aws_access_key_id": username,
        "aws_secret_access_key": password,
        "region_name": "us-east-1",
    }


"""Opt-in live-source test, isolated from the project's actual MinIO buckets."""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import yaml

from src.common.config import PROJECT_ROOT, load_storage_config
from src.common.minio_client import create_minio_client, list_object_keys


pytestmark = pytest.mark.skipif(os.getenv("ADSBD_RUN_LIVE_PIPELINE") != "1", reason="Set ADSBD_RUN_LIVE_PIPELINE=1 to fetch real sources")


def test_real_sources_reach_all_trusted_modalities_and_resume(tmp_path):
    client = create_minio_client()
    storage = load_storage_config()
    token = "adsbd-pipeline-test-" + uuid.uuid4().hex
    buckets = [token + "-" + zone for zone in ("landing", "formatted", "trusted")]
    for zone, bucket in zip(("landing", "formatted", "trusted"), buckets):
        storage["buckets"][zone] = bucket
    config = tmp_path / "storage.yml"
    config.write_text(yaml.safe_dump(storage), encoding="utf-8")
    reports = tmp_path / "reports"
    command = [sys.executable, "-m", "src.pipelines.run_pipeline", "--storage-config", str(config),
               "--max-games", "1", "--screenshots-per-game", "1", "--max-audio-files", "1",
               "--audio-max-items", "15", "--rawg-output", str(tmp_path / "images"),
               "--text-output", str(tmp_path / "text"), "--audio-output", str(tmp_path / "music"),
               "--report-dir", str(reports)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        first = subprocess.run(command, cwd=PROJECT_ROOT, env=env, timeout=1200)
        assert first.returncode == 0
        report = json.loads(sorted(reports.glob("run_*.json"))[-1].read_text())
        assert all(report["trusted"].values())
        assert len(report["steps"]) == 17
        before = {bucket: {key: client.head_object(Bucket=bucket, Key=key)["ETag"]
                           for key in list_object_keys(client, bucket)} for bucket in buckets}
        # Restoring a missing local image report is essential for a complete resume.
        (reports / "images/quality.csv").unlink()
        second = subprocess.run([*command, "--skip-collection"], cwd=PROJECT_ROOT, env=env, timeout=1200)
        assert second.returncode == 0
        assert (reports / "images/quality.csv").is_file()
        after = {bucket: {key: client.head_object(Bucket=bucket, Key=key)["ETag"]
                          for key in list_object_keys(client, bucket)} for bucket in buckets}
        assert {b: set(keys) for b, keys in before.items()} == {b: set(keys) for b, keys in after.items()}
        # Data bytes stay identical. Quality report timestamps may legitimately change.
        for bucket, objects in before.items():
            for key, digest in objects.items():
                if key.lower().endswith((".flac", ".parquet", ".jpg")):
                    assert after[bucket][key] == digest
    finally:
        existing = {entry["Name"] for entry in client.list_buckets()["Buckets"]}
        for bucket in buckets:
            assert bucket.startswith(token + "-")
            if bucket not in existing:
                continue
            for key in list_object_keys(client, bucket):
                client.delete_object(Bucket=bucket, Key=key)
            client.delete_bucket(Bucket=bucket)

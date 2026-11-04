"""Opt-in full-size recovery test using real backup data and fresh storage."""

import json
import os
import time
import uuid

import pytest
import yaml

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client, list_object_keys
from src.pipelines import run_pipeline as pipeline
from src.pipelines.backup_sources import validate_backup


pytestmark = pytest.mark.skipif(os.getenv("ADSBD_RUN_BACKUP_PIPELINE") != "1",
                              reason="Set ADSBD_RUN_BACKUP_PIPELINE=1 for the real 50/50/16 recovery run")


def test_all_source_failures_use_backup_through_verified_embeddings(tmp_path, monkeypatch):
    backup = pipeline.PROJECT_ROOT / "backup_data"
    assert validate_backup(backup)["counts"] == {"descriptions": 50, "images": 50, "audio": 16}
    storage, client = load_storage_config(), create_minio_client()
    token = "adsbd-backup-test-" + uuid.uuid4().hex[:16]
    buckets = [token + "-" + zone for zone in ("landing", "formatted", "trusted")]
    for zone, bucket in zip(("landing", "formatted", "trusted"), buckets):
        storage["buckets"][zone] = bucket
    config = tmp_path / "storage.yml"
    config.write_text(yaml.safe_dump(storage), encoding="utf-8")
    monkeypatch.setenv("ADSBD_STORAGE_CONFIG", str(config))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    execute = pipeline.execute
    failed = []
    def source_failure(command, env):
        if len(command) > 2 and command[2] in {"src.data_collection.collect_rawg_images",
                                              "src.ingestion.text_to_temporal_landing",
                                              "src.data_collection.collect_opengameart_audio"}:
            failed.append(command[2])
            print(f"[TEST] Simulated unavailable source: {command[2]}", flush=True)
            return 2
        return execute(command, env)
    monkeypatch.setattr(pipeline, "execute", source_failure)
    reports = tmp_path / "reports"
    started = time.monotonic()
    try:
        code = pipeline.main(["--backup-dir", str(backup), "--storage-config", str(config),
                              "--rawg-output", str(tmp_path / "rawg"), "--text-output", str(tmp_path / "texts"),
                              "--audio-output", str(tmp_path / "audio"), "--db-path", str(tmp_path / "chroma"),
                              "--report-dir", str(reports)])
        assert code == 0
        report = json.loads(next(reports.glob("run_*.json")).read_text())
        assert len(report["steps"]) == 24
        assert {row["source"] for row in report["backup_fallbacks"]} == {"rawg", "opengameart"}
        assert "src.ingestion.text_to_temporal_landing" not in failed
        verification = json.loads((reports / "exploitation.json").read_text())
        assert verification["latest_descriptions"] == 50
        assert verification["collections"]["texts_clap"] == 50
        assert verification["collections"]["audio_clap_center10_v1"] == 16
        assert verification["task3_data_ready"]
        landing = storage["buckets"]["landing"]
        assert sum(key.endswith(".json") for key in list_object_keys(client, landing, "persistent/texts/rawg/")) == 50
        assert len(list_object_keys(client, landing, "persistent/images/")) == 50
        evidence = {"input_counts": {"descriptions": 50, "images": 50, "audio": 16},
                    "source_failures": "simulated", "model_cache": "existing pinned weights; no data or vector reuse",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "run": report, "verification": verification}
        output = pipeline.PROJECT_ROOT / "reports/pipeline/backup_validation_50_50_16.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    finally:
        existing = {entry["Name"] for entry in client.list_buckets()["Buckets"]}
        for bucket in buckets:
            assert bucket.startswith(token + "-")
            if bucket in existing:
                for key in list_object_keys(client, bucket):
                    client.delete_object(Bucket=bucket, Key=key)
                client.delete_bucket(Bucket=bucket)

"""Opt-in full-size recovery test using real backup data and fresh storage."""

import json
import csv
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
        with (reports / "audio/formatting.csv").open(encoding="utf-8", newline="") as stream:
            conversions = list(csv.DictReader(stream))
        attenuated = [row for row in conversions if "applied gain=" in row["message"]]
        assert len(attenuated) == 2
        assert all("prevent clipping" in row["message"] for row in attenuated)
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


def test_real_minio_retirement_removes_duplicate_and_its_vector(tmp_path, monkeypatch):
    """Exercise changing quality decisions against actual storage and Chroma."""
    import io
    import numpy as np
    import chromadb
    from PIL import Image
    from src.trusted.images import build_trusted_images as trusted
    from src.exploitation.images import build_embeddings as images
    client = create_minio_client()
    token = "adsbd-retirement-test-" + uuid.uuid4().hex[:16]
    buckets = [token + "-formatted", token + "-trusted"]
    config = {"buckets": dict(zip(("formatted", "trusted"), buckets))}
    monkeypatch.setattr(trusted, "load_storage_config", lambda: config)
    monkeypatch.setattr(trusted, "create_minio_client", lambda: client)
    encoded = io.BytesIO()
    Image.fromarray(np.random.default_rng(7).integers(0, 256, (480, 640, 3), dtype=np.uint8)).save(encoded, format="JPEG")
    body = encoded.getvalue()
    try:
        for bucket in buckets:
            client.create_bucket(Bucket=bucket)
        client.put_object(Bucket=buckets[0], Key="images/b.jpg", Body=body)
        assert trusted.build_trusted_images(dry_run=False, csv_output=tmp_path / "quality.csv") == 0
        client.put_object(Bucket=buckets[0], Key="images/a.jpg", Body=body)
        assert trusted.build_trusted_images(dry_run=False, csv_output=tmp_path / "quality.csv") == 0
        assert list_object_keys(client, buckets[1], "images/") == ["images/a.jpg"]
        assert client.get_object(Bucket=buckets[0], Key="images/b.jpg")["Body"].read() == body
        asset = images.Asset(buckets[1], "images/a.jpg", client.head_object(Bucket=buckets[1], Key="images/a.jpg")["ETag"].strip('"'), {})
        db = chromadb.PersistentClient(path=str(tmp_path / "retirement_chroma"))
        collection = db.create_collection("retirement_test", embedding_function=None)
        live_id = images.record_id(asset, "test")
        collection.upsert(ids=[live_id, "retired"], embeddings=[[1., 0.], [0., 1.]],
                          metadatas=[{"asset_id": asset.id, "config_sha256": "test"},
                                     {"asset_id": "images/b.jpg", "config_sha256": "test"}])
        class Encoder:
            dimension = 2
        assert images.run(client, [asset], Encoder(), collection, "test", batch_size=1, prune=True) == 0
        assert collection.get()["ids"] == [live_id]
    finally:
        existing = {row["Name"] for row in client.list_buckets()["Buckets"]}
        for bucket in buckets:
            if bucket in existing:
                for key in list_object_keys(client, bucket):
                    client.delete_object(Bucket=bucket, Key=key)
                client.delete_bucket(Bucket=bucket)


def test_real_minio_upgrades_legacy_clipped_audio_and_refreshes_trusted(tmp_path):
    """Migrate a tracked v1 derivative while keeping its original immutable."""
    import hashlib
    import subprocess
    from src.formatting.audio import formatting_audio as formatting
    from src.trusted.audio import analyze_audio_quality as quality
    from src.trusted.audio import trusted_audio as trusted
    client = create_minio_client()
    token = "adsbd-audio-upgrade-test-" + uuid.uuid4().hex[:16]
    buckets = [token + "-" + zone for zone in ("landing", "formatted", "trusted")]
    source = pipeline.PROJECT_ROOT / "backup_data/audio/raw/files/3922/cave%20themeb4.ogg"
    body = source.read_bytes()
    key = "persistent/audio/opengameart$music$05-11-2026$3922$cave%20themeb4.ogg"
    destination = formatting.destination_key_for(key, "persistent")
    legacy = tmp_path / "legacy.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0",
                    "-c:a", "flac", "-sample_fmt", "s16", str(legacy)], check=True)
    old_body = legacy.read_bytes()
    old_sha = hashlib.sha256(old_body).hexdigest()
    try:
        for bucket in buckets:
            client.create_bucket(Bucket=bucket)
        original_sha = hashlib.sha256(body).hexdigest()
        client.put_object(Bucket=buckets[0], Key=key, Body=body, Metadata={"sha256": original_sha})
        signature = formatting.source_signature(buckets[0], key, client.head_object(Bucket=buckets[0], Key=key))
        client.put_object(Bucket=buckets[1], Key=destination, Body=old_body, Metadata={
            "format_version": "audio-flac-v1", "source_signature": signature,
            "source_sha256": original_sha, "source_key_sha256": hashlib.sha256(key.encode()).hexdigest()})
        client.put_object(Bucket=buckets[2], Key=destination, Body=old_body,
                          Metadata=trusted.metadata_for({"source_key": destination, "sha256": old_sha}, "copy"))
        converted = formatting.process_one(client, buckets[0], buckets[1], key, "persistent", overwrite=False)
        assert converted["status"] == "converted" and "applied gain=" in converted["message"]
        row = quality.profile_object(client, buckets[1], destination, {})
        assert float(row["peak_dbfs"]) < -0.09
        published = trusted.process_one(client, buckets[1], buckets[2], row)
        assert published["status"] == "copied"
        new_body = client.get_object(Bucket=buckets[1], Key=destination)["Body"].read()
        assert new_body != old_body
        assert client.get_object(Bucket=buckets[2], Key=destination)["Body"].read() == new_body
        assert client.get_object(Bucket=buckets[0], Key=key)["Body"].read() == body
        assert formatting.process_one(client, buckets[0], buckets[1], key, "persistent", overwrite=False)["status"] == "skipped"
        assert trusted.process_one(client, buckets[1], buckets[2], row)["status"] == "skipped"
    finally:
        existing = {row["Name"] for row in client.list_buckets()["Buckets"]}
        for bucket in buckets:
            if bucket in existing:
                for name in list_object_keys(client, bucket):
                    client.delete_object(Bucket=bucket, Key=name)
                client.delete_bucket(Bucket=bucket)

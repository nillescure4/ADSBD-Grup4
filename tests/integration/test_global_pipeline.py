"""Opt-in live-source test, isolated from the project's actual MinIO buckets."""

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
import yaml

from src.common.config import PROJECT_ROOT, load_storage_config
from src.common.minio_client import create_minio_client, list_object_keys


@pytest.mark.skipif(os.getenv("ADSBD_RUN_LIVE_PIPELINE") != "1", reason="Set ADSBD_RUN_LIVE_PIPELINE=1 to fetch real sources")
def test_real_sources_reach_all_trusted_modalities_and_resume(tmp_path):
    exercise_pipeline(tmp_path)


@pytest.mark.skipif(os.getenv("ADSBD_RUN_CAPTURED_PIPELINE") != "1", reason="Set ADSBD_RUN_CAPTURED_PIPELINE=1 to reuse real local source captures")
def test_captured_sources_reach_embeddings_and_resume(tmp_path):
    exercise_pipeline(tmp_path, prepare=prepare_captured_sources)


def exercise_pipeline(tmp_path, prepare=None):
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
               "--max-games", "2", "--screenshots-per-game", "1", "--max-audio-files", "2",
               "--audio-max-items", "15", "--rawg-output", str(tmp_path / "images"),
               "--text-output", str(tmp_path / "text"), "--audio-output", str(tmp_path / "music"),
               "--report-dir", str(reports), "--db-path", str(tmp_path / "chroma")]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        if prepare:
            for bucket in buckets:
                client.create_bucket(Bucket=bucket)
            prepare(tmp_path, client, storage)
            command.append("--skip-collection")
        first = subprocess.run(command, cwd=PROJECT_ROOT, env=env, timeout=1200)
        assert first.returncode == 0
        report = json.loads(sorted(reports.glob("run_*.json"))[-1].read_text())
        assert all(report["trusted"].values())
        assert len(report["steps"]) == (20 if prepare else 23)
        verification = json.loads((reports / "exploitation.json").read_text())
        assert len(verification["collections"]) == 4
        assert all(verification["collections"].values())
        assert verification["games_with_screenshots"] > 0
        assert len(verification["retrieval_checks"]) == 8
        if prepare:
            assert verification["task3_data_ready"]
        import chromadb
        db = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
        indexed = {name: db.get_collection(name).get(include=["embeddings", "metadatas", "documents"])
                   for name in verification["collections"]}
        before = {bucket: {key: client.head_object(Bucket=bucket, Key=key)["ETag"]
                           for key in list_object_keys(client, bucket)} for bucket in buckets}
        # Restoring a missing local image report is essential for a complete resume.
        (reports / "images/quality.csv").unlink()
        second = subprocess.run([*command, "--skip-collection"], cwd=PROJECT_ROOT, env=env, timeout=1200)
        assert second.returncode == 0
        assert (reports / "images/quality.csv").is_file()
        for name, previous in indexed.items():
            current = db.get_collection(name).get(include=["embeddings", "metadatas", "documents"])
            assert current["ids"] == previous["ids"]
            assert current["metadatas"] == previous["metadatas"]
            assert current["documents"] == previous["documents"]
            import numpy as np
            assert np.array_equal(current["embeddings"], previous["embeddings"])
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


def prepare_captured_sources(root, client, storage):
    """Reuse unmodified real downloads; no substitute responses or generated media."""
    source = PROJECT_ROOT / "data/landing/search_50_100"
    text_root = source / "texts"
    text_rows = {row["source_game_id"]: row for row in
                 (json.loads(line) for line in (text_root / "manifests/text_sources.jsonl").read_text(encoding="utf-8").splitlines())}
    image_rows = [json.loads(line) for line in (source / "image_sources.jsonl").read_text(encoding="utf-8").splitlines()]
    selected, games, seen = [], [], set()
    for row in image_rows:
        game = row["game_id"]
        if game not in text_rows or game in seen:
            continue
        image = source / "images" / row["filename"]
        raw = image.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
        destination = root / "images/images" / image.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        selected.append({"source_game_id": game, "name": row["game_name"], "source_url": row["source_url"],
                         "local_path": "images/" + image.name, "sha256": row["sha256"]})
        seen.add(game)
        games.append({"source_game_id": game, "name": row["game_name"]})
        if len(seen) == 8:
            break
    assert len(seen) == 8
    manifests = root / "images/manifests"
    manifests.mkdir(parents=True)
    for name, rows in (("images", selected), ("games", games)):
        (manifests / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    landing = storage["buckets"]["landing"]
    prefix = storage["landing"]["temporal_prefix"].strip("/") + "/" + storage["landing"]["texts_prefix"].strip("/")
    for game in seen:
        row = text_rows[game]
        raw = (text_root / row["raw_object_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
        client.put_object(Bucket=landing, Key=f"{prefix}/{game}.json", Body=raw)
    client.put_object(Bucket=landing, Key=f"{prefix}/manifests/text_sources.jsonl",
                      Body="".join(json.dumps(text_rows[game]) + "\n" for game in sorted(seen)).encode("utf-8"))
    music = PROJECT_ROOT / "data/audio/search_16"
    for row in [json.loads(line) for line in (music / "manifests/files.jsonl").read_text(encoding="utf-8").splitlines()][:2]:
        relative = Path(row["local_path"].replace("\\", "/"))
        raw = (music / relative).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
        destination = root / "music" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)

"""Opt-in real OpenGameArt → zones → CLAP → application retrieval validation."""

import hashlib
import csv
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.getenv("ADSBD_RUN_AUDIO_SEARCH_TESTS") != "1", reason="Requires the real 16-track corpus, MinIO and CLAP weights")


def test_real_audio_pipeline_and_search(tmp_path):
    import torch
    from src.app.audio_search import (
        connect_audio, preview_audio, require_audio_tools, search_audio,
        search_audio_text, uploaded_query, validate_audio_index,
    )
    from src.common.config import load_storage_config
    from src.common.minio_client import create_minio_client, list_object_keys
    from src.exploitation.audio.embedding_audio import run_pipeline
    from src.exploitation.audio.search_audio import ClapTextEncoder
    from src.formatting.audio.formatting_audio import run_formatting
    from src.ingestion.audio.audio_landing import ingest_audio
    from src.ingestion.audio.audio_persistent import promote_audio, persistent_index
    from src.trusted.audio.trusted_audio import run_trusted

    torch.set_num_threads(4)
    require_audio_tools()
    root = Path("data/audio/search_16")
    files = [json.loads(line) for line in (root / "manifests/files.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(files) == 16 and len({row["file_url"] for row in files}) == 16
    client, storage = create_minio_client(), load_storage_config()
    landing, formatted, trusted = (storage["buckets"][zone] for zone in ("landing", "formatted", "trusted"))
    persistent = persistent_index(client, landing, storage["landing"]["persistent_prefix"])
    catalogue = connect_audio()
    assert catalogue.count() == 16
    validate_audio_index(catalogue)
    records = catalogue.get(include=["embeddings", "metadatas"])
    vectors = np.asarray(records["embeddings"], dtype=np.float32)
    assert vectors.shape == (16, 512) and np.isfinite(vectors).all()
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1., atol=1e-4)
    metadata = dict(zip(records["ids"], records["metadatas"]))
    with Path("reports/audio_16_quality.csv").open(encoding="utf-8", newline="") as stream:
        quality_rows = {row["source_key"]: row for row in csv.DictReader(stream)}
    for row in files:
        local = root / row["local_path"]
        assert hashlib.sha256(local.read_bytes()).hexdigest() == row["sha256"]
        identity = (local.parent.name, local.name)
        key, = persistent[identity]
        raw = client.get_object(Bucket=landing, Key=key)["Body"].read()
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
        target = "audio/" + key.split("/audio/", 1)[1] + ".flac"
        for bucket in (formatted, trusted):
            body = client.get_object(Bucket=bucket, Key=target)["Body"].read()
            head = client.head_object(Bucket=bucket, Key=target)
            expected = head["Metadata"].get("trusted-sha256") or quality_rows[target]["sha256"]
            assert expected and hashlib.sha256(body).hexdigest() == expected
        meta, = [item for item in metadata.values() if item["source_key"] == target]
        with wave.open(io.BytesIO(preview_audio(client, meta, trusted))) as player:
            assert player.getframerate() == 48000
            assert 0 < player.getnframes() <= 480000

    # Check every indexed self-query against exhaustive cosine products.
    for index, vector in enumerate(vectors):
        hits = search_audio(catalogue, vector.tolist(), top_k=16)
        expected = vectors @ vector
        assert hits[0]["score"] == pytest.approx(1., abs=1e-5)
        for hit in hits:
            position = records["ids"].index(hit["vector_id"])
            assert hit["score"] == pytest.approx(float(expected[position]), abs=1e-5)
        excluded = metadata[records["ids"][index]]["source_key"]
        assert all(hit["source_key"] != excluded for hit in search_audio(catalogue, vector.tolist(), top_k=16, source_key=excluded))

    encoder = ClapTextEncoder()
    examples = []
    for text in ("Rain and thunder in a dark forest", "Fast energetic battle music", "Relaxing piano music"):
        hits = search_audio_text(catalogue, encoder, text, top_k=3)
        assert len(hits) == 3
        examples.append({"query": text, "results": [{"key": item["source_key"], "score": item["score"]} for item in hits]})
    first = root / files[0]["local_path"]
    query = uploaded_query(first.read_bytes(), first.suffix, encoder)
    hits = search_audio(catalogue, query, top_k=3)
    assert hits[0]["source_key"].endswith("$" + first.name + ".flac")
    original_upload_score = hits[0]["score"]
    trusted_bytes = client.get_object(Bucket=trusted, Key=hits[0]["source_key"])["Body"].read()
    trusted_query = uploaded_query(trusted_bytes, ".flac", encoder)
    assert search_audio(catalogue, trusted_query, top_k=1)[0]["score"] > .9999

    # Re-run each stage: existing data and vectors must remain unchanged.
    ingestion = ingest_audio(root / "raw/files", client=client, bucket=landing,
                             temporal_prefix="temporal", persistent_prefix="persistent", limit=None)
    assert ingestion.skipped == 16 and ingestion.uploaded == ingestion.failed == 0
    promotion = promote_audio(client, bucket=landing, temporal_prefix="temporal", persistent_prefix="persistent", limit=None)
    assert promotion.selected == 0
    formatting = run_formatting(client, storage, "persistent/audio/", report=tmp_path / "formatted.csv")
    assert formatting["skipped"] == 16 and formatting["failed"] == 0
    quality = Path("reports/audio_16_quality.csv")
    publication = run_trusted(client, storage, quality, report=tmp_path / "trusted.csv")
    assert publication["skipped"] == 16 and publication["failed"] == 0
    def no_embedding(samples):
        pytest.fail("Unchanged audio must not be encoded again")
    indexing = run_pipeline(client, storage, limit=16, dry_run=False,
        chroma_path=Path("data/exploitation/chroma"), report=tmp_path / "index.csv",
        encoder_factory=lambda: SimpleNamespace(model_revision=encoder.model_revision, embed_audio=no_embedding),
        collection_factory=lambda path: catalogue)
    assert indexing["skipped"] == 16 and indexing["indexed"] == indexing["failed"] == 0
    after = catalogue.get(ids=records["ids"], include=["embeddings", "metadatas"])
    by_id = {item: (vector, meta) for item, vector, meta in zip(after["ids"], after["embeddings"], after["metadatas"])}
    for index, item in enumerate(records["ids"]):
        assert np.array_equal(vectors[index], by_id[item][0]) and metadata[item] == by_id[item][1]
    assert not list_object_keys(client, landing, "temporal/audio/")
    report = {"tracks": 16, "dimension": 512, "sources_verified": 16,
              "playable_excerpts_verified": 16, "self_queries_verified": 16,
              "upload_query_verified": True, "original_upload_top_score": original_upload_score,
              "incremental_stages_verified": True, "text_queries": examples}
    Path("data/exploitation/audio_validation_16.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

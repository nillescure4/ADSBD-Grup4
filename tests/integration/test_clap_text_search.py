"""Opt-in validation of the real CLAP description corpus and audio queries."""

from functools import lru_cache
import json
import os
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.getenv("ADSBD_RUN_CLAP_TEXT_TESTS") != "1",
                              reason="Requires the real Trusted corpus, CLAP indexes and cached weights")


def test_real_clap_texts_sources_audio_queries_and_incremental_indexing():
    import chromadb
    import torch
    from src.app.clap_text_search import connect_clap_texts, search_clap_descriptions
    from src.app.audio_search import connect_audio, search_audio
    from src.app.trusted_details import load_snapshot, select_game
    from src.common.config import load_storage_config
    from src.common.minio_client import create_minio_client
    from src.exploitation.text.build_embeddings import load_assets
    from src.exploitation.text.build_clap_embeddings import DescriptionEncoder, run

    torch.set_num_threads(4)
    client, storage = create_minio_client(), load_storage_config()
    assets = load_assets(client, storage, latest_only=True)
    catalogue, audio = connect_clap_texts(), connect_audio()
    records = catalogue.get(include=["documents", "metadatas", "embeddings"])
    vectors = np.asarray(records["embeddings"], dtype=np.float32)
    assert vectors.shape == (len(records["ids"]), 512) and np.isfinite(vectors).all()
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1., atol=1e-4)
    @lru_cache(maxsize=8)
    def snapshot(bucket, key, digest):
        return load_snapshot(client, bucket, key, digest)
    by_game = {}
    for document, meta in zip(records["documents"], records["metadatas"]):
        row = select_game(snapshot(meta["trusted_bucket"], meta["trusted_object_key"], meta["trusted_object_sha256"]),
                          {"game_id": meta["game_id"], "text": document, "metadata": meta})
        assert 0 < meta["token_count"] <= 512
        by_game.setdefault(meta["game_id"], []).append((meta, document))
        assert row["id"] == meta["game_id"]
    assert len(by_game) == len(assets) == 100
    for asset in assets:
        [item] = by_game[asset.game_id]
        meta, document = item
        assert asset.text[:meta["char_end"]] == document and meta["char_start"] == 0
        assert meta["truncated"] == (document != asset.text)

    audio_records = audio.get(include=["embeddings"])
    assert len(audio_records["ids"]) == 16
    for vector in audio_records["embeddings"]:
        hits = search_clap_descriptions(catalogue, vector, top_k=100)
        assert len(hits) == 100 and len({hit["game_id"] for hit in hits}) == 100
        direct = vectors @ np.asarray(vector)
        expected = {}
        for score, meta in zip(direct, records["metadatas"]):
            expected[meta["game_id"]] = max(expected.get(meta["game_id"], -1.), float(score))
        assert all(hit["score"] == pytest.approx(expected[hit["game_id"]], abs=1e-5) for hit in hits)

    encoder = DescriptionEncoder()
    query = encoder.encode(records["documents"][0])
    hits = search_clap_descriptions(catalogue, query, top_k=100)
    own_game = records["metadatas"][0]["game_id"]
    assert next(hit for hit in hits if hit["game_id"] == own_game)["score"] > .9999
    assert len(search_audio(audio, query, top_k=3)) == 3

    # An unchanged rerun must not even load the model weights.
    incremental = DescriptionEncoder()
    outcome = run(assets, incremental, catalogue)
    assert outcome["skipped"] == 100 and outcome["encoded"] == outcome["updated"] == 0
    assert incremental.encoder is None
    after = catalogue.get(ids=records["ids"], include=["embeddings", "metadatas"])
    actual = dict(zip(after["ids"], zip(after["embeddings"], after["metadatas"])))
    assert len(actual) == len(records["ids"])
    for item, vector, meta in zip(records["ids"], vectors, records["metadatas"]):
        assert np.array_equal(vector, actual[item][0]) and meta == actual[item][1]
    db = chromadb.PersistentClient(path="data/exploitation/chroma")
    assert db.get_collection("images_jina_clip_v2").count() == 50
    assert db.get_collection("texts_jina_clip_v2").count() == 105
    assert audio.count() == 16
    Path("data/exploitation/clap_text_validation.json").write_text(json.dumps({
        "games": 100, "vectors": len(records["ids"]), "dimensions": 512,
        "source_prefixes_verified": True, "truncated_descriptions": sum(meta["truncated"] for meta in records["metadatas"]),
        "audio_queries_verified": 16,
        "incremental": outcome, "jina_and_audio_collections_preserved": True}, indent=2), encoding="utf-8")

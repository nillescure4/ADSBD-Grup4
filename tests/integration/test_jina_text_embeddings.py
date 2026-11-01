"""Opt-in real Jina, Trusted and ChromaDB verification; no source writes."""

import os

import pytest

from src.exploitation.text import build_embeddings as embeddings
from src.exploitation.jina_clip import JinaEncoder


pytestmark = pytest.mark.skipif(os.getenv("ADSBD_RUN_JINA_MODEL_TESTS") != "1",
                              reason="Set ADSBD_RUN_JINA_MODEL_TESTS=1 for the real model and MinIO test")


def test_real_model_full_descriptions_persistence_queries_and_incremental_reuse(tmp_path):
    import chromadb
    import torch
    from PIL import Image
    assets = embeddings.load_assets(embeddings.create_minio_client(), embeddings.load_storage_config())
    assert assets and any(asset.text.strip() for asset in assets)
    encoder = JinaEncoder()
    settings, fingerprint = embeddings.configuration(encoder, False)
    db = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    collection = embeddings.open_collection(db, embeddings.COLLECTION, settings, fingerprint)
    assert embeddings.run(assets, encoder, collection, fingerprint, batch_size=2) == 0
    expected = {asset.id: asset for asset in assets if asset.text.strip()}
    stored = collection.get(include=["documents", "metadatas", "embeddings"])
    assert len(stored["ids"]) == len(expected)
    for document, metadata, vector in zip(stored["documents"], stored["metadatas"], stored["embeddings"]):
        assert document == expected[metadata["asset_id"]].text
        assert embeddings.valid_vector(vector, 1024)
        assert metadata["model_revision"] == embeddings.MODEL_REVISION
        assert metadata["token_count"] == encoder.count_tokens(document)

    unchanged = JinaEncoder()
    assert embeddings.run(assets, unchanged, collection, fingerprint, batch_size=2) == 0
    assert unchanged.model is None
    again = collection.get(include=["documents", "metadatas", "embeddings"])
    assert again["ids"] == stored["ids"] and again["metadatas"] == stored["metadatas"]
    assert (again["embeddings"] == stored["embeddings"]).all()

    document = stored["documents"][0]
    vector = encoder.encode([document])[0]
    nearest = collection.query(query_embeddings=[vector], n_results=1, include=["documents", "distances"])
    assert nearest["documents"][0][0] == document
    assert nearest["distances"][0][0] == pytest.approx(0, abs=1e-5)
    query = encoder.encode_query("A puzzle game with portals")
    assert embeddings.valid_vector(query, 1024)
    assert collection.query(query_embeddings=[query], n_results=1)["ids"][0]

    # Exercise text beyond Jina encode_text's default 512-token truncation point.
    long_text = "Explore a mysterious world with cooperative puzzles and ancient ruins. " * 70
    assert 512 < encoder.count_tokens(long_text) <= 8192
    assert embeddings.valid_vector(encoder.encode([long_text])[0], 1024)

    # A shape/normalization probe of the paired image tower, not a quality benchmark
    # or a test of the teammate's not-yet-published image collection.
    with torch.inference_mode():
        image_vectors = encoder.load_model().encode_image(
            [Image.new("RGB", (512, 512), "navy")], truncate_dim=None,
            normalize_embeddings=True, convert_to_tensor=True, convert_to_numpy=False,
            show_progress_bar=False,
        ).float().cpu().tolist()
    assert embeddings.valid_vector(image_vectors[0], 1024)

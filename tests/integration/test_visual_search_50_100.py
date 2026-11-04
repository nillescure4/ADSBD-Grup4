"""Opt-in real retrieval validation: exactly 50 images and 100 descriptions."""

import json
import os
from pathlib import Path

import numpy as np
import pytest

from src.app.text_search import connect
from src.app.visual_search import (connect_images, load_image, rank_vector, search_from_image,
                                   search_from_text, validate_collection)
from src.app.trusted_details import load_snapshot, select_game
from src.common.minio_client import create_minio_client
from src.exploitation.images import build_embeddings as image_pipeline
from src.exploitation.jina_clip import JinaEncoder
from src.exploitation.text.build_embeddings import valid_vector


pytestmark = pytest.mark.skipif(os.getenv("ADSBD_RUN_VISUAL_SEARCH_TESTS") != "1",
                              reason="Set ADSBD_RUN_VISUAL_SEARCH_TESTS=1 for the real 50/100 dataset")


class LatestDescriptions:
    """A read-only Chroma view selecting exactly the 100 latest descriptions."""

    def __init__(self, collection):
        self.collection = collection
        self.metadata, self.configuration = collection.metadata, collection.configuration
        latest = {}
        for row in collection.get(include=["metadatas"])["metadatas"]:
            if row["game_id"] not in latest or row["collection_date"] > latest[row["game_id"]]["collection_date"]:
                latest[row["game_id"]] = row
        assert len(latest) == 100
        self.where = {"asset_id": {"$in": [row["asset_id"] for row in latest.values()]}}

    def get(self, **kwargs):
        return self.collection.get(where=self.where, **kwargs)

    def query(self, **kwargs):
        return self.collection.query(where=self.where, **kwargs)


def test_real_50_images_100_descriptions_and_all_search_directions():
    import torch
    torch.set_num_threads(4)
    client = create_minio_client()
    text = LatestDescriptions(connect())
    images = connect_images()
    encoder = JinaEncoder()
    validate_collection(text, encoder, "text")
    validate_collection(images, encoder, "image")
    texts = text.get(include=["documents", "metadatas", "embeddings"])
    pictures = images.get(include=["metadatas", "embeddings"])
    assert len(texts["ids"]) == 100 and len(pictures["ids"]) == 50
    assert len({row["game_id"] for row in pictures["metadatas"]}) == 50
    assert all(valid_vector(vector, 1024) for vector in [*texts["embeddings"], *pictures["embeddings"]])

    snapshots = {}
    for document, metadata in zip(texts["documents"], texts["metadatas"]):
        key = (metadata["trusted_bucket"], metadata["trusted_object_key"], metadata["trusted_object_sha256"])
        if key not in snapshots:
            snapshots[key] = load_snapshot(client, *key)
        select_game(snapshots[key], {"game_id": metadata["game_id"], "text": document, "metadata": metadata})
    decoded = []
    for row in pictures["metadatas"]:
        image = load_image(client, row, "3.trusted-zone")
        if len(decoded) < 3:
            decoded.append(image)
        else:
            image.close()

    image_to_text_hits, text_to_image_hits = [], []
    text_position = {row["game_id"]: index for index, row in enumerate(texts["metadatas"])}
    for item, metadata, vector in zip(pictures["ids"], pictures["metadatas"], pictures["embeddings"]):
        hits = rank_vector(images, vector, modality="image", top_k=1)
        assert hits[0]["id"] == item and hits[0]["score"] == pytest.approx(1, abs=1e-5)
        hits = rank_vector(text, vector, modality="text", top_k=5)
        expected = texts["embeddings"] @ np.asarray(vector)
        assert hits[0]["score"] == pytest.approx(float(expected.max()), abs=1e-5)
        image_to_text_hits.append(metadata["game_id"] in {hit["game_id"] for hit in hits})
        paired = texts["embeddings"][text_position[metadata["game_id"]]]
        hits = rank_vector(images, paired, modality="image", top_k=5)
        text_to_image_hits.append(metadata["game_id"] in {hit["metadata"]["game_id"] for hit in hits})
    for metadata, vector in zip(texts["metadatas"], texts["embeddings"]):
        hits = rank_vector(text, vector, modality="text", top_k=100)
        own = next(hit for hit in hits if hit["game_id"] == metadata["game_id"])
        assert own["score"] == pytest.approx(1, abs=1e-5)
        hits = rank_vector(images, vector, modality="image", top_k=5)
        expected = pictures["embeddings"] @ np.asarray(vector)
        assert hits[0]["score"] == pytest.approx(float(expected.max()), abs=1e-5)

    examples = []
    for query in ("A western landscape with horses and countryside", "A futuristic science fiction laboratory", "A fantasy adventure with swords and monsters"):
        hits = search_from_text(text, images, encoder, query, targets=["text", "image"], top_k=5)
        assert len(hits["text"]) == len(hits["image"]) == 5
        examples.append({"query": query, "first_game": hits["text"][0]["game_id"],
                         "first_image_game": hits["image"][0]["metadata"]["game_id"]})
    for index in range(3):
        hits = search_from_image(text, images, encoder, decoded[index], targets=["text", "image"], top_k=5)
        assert hits["image"][0]["id"] == pictures["ids"][index]
        assert hits["image"][0]["score"] == pytest.approx(1, abs=1e-5)
        assert len(hits["text"]) == 5

    # Repeating ingestion must not compute or modify any of the 50 image vectors.
    from src.common.config import load_storage_config
    unloaded = JinaEncoder()
    assets = image_pipeline.load_assets(client, load_storage_config())
    assert image_pipeline.run(client, assets, unloaded, images, images.metadata["config_sha256"], batch_size=2, prune=False) == 0
    assert unloaded.model is None
    again = images.get(include=["metadatas", "embeddings"])
    assert again["ids"] == pictures["ids"] and again["metadatas"] == pictures["metadatas"]
    assert np.array_equal(again["embeddings"], pictures["embeddings"])
    report = {"images": 50, "descriptions": 100, "dimensions": 1024,
              "image_sources_verified": 50, "text_sources_verified": 100,
              "image_self_queries": 50, "description_self_queries": 100,
              "image_to_description_queries": 50, "description_vector_to_image_queries": 100,
              "image_to_description_game_recall_at_5": float(np.mean(image_to_text_hits)),
              "description_vector_to_image_game_recall_at_5": float(np.mean(text_to_image_hits)),
              "note": "Source game association is a diagnostic, not a semantic relevance guarantee. Description-vector probes use stored document vectors; natural-language query encoding is checked separately.",
              "live_text_queries": examples, "live_image_queries": 3, "incremental_images_unchanged": 50}
    path = Path("data/exploitation/text_image_validation_50_100.json")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

import pytest

from src.app import text_search as service
from src.exploitation.text.build_embeddings import configuration


class Encoder:
    settings = {"model": "test"}
    budget = 5

    def count_tokens(self, text):
        return len(text)

    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def encode_query(self, text):
        return [1.0, 0.0]


class Collection:
    metadata = {"config_sha256": configuration(Encoder(), False)[1]}
    configuration = {"hnsw": {"space": "cosine"}}

    def __init__(self):
        self.rows = [
            {"game_id": 1, "collection_date": "2026-10-20", "asset_id": "1:old"},
            {"game_id": 1, "collection_date": "2026-10-22", "asset_id": "1:new"},
            {"game_id": 2, "collection_date": "2026-10-22", "asset_id": "2:new"},
            {"game_id": 2, "collection_date": "2026-10-22", "asset_id": "2:new"},
        ]

    def get(self, **kwargs):
        return {"ids": list(range(len(self.rows))), "metadatas": self.rows}

    def query(self, query_embeddings, n_results, **kwargs):
        assert n_results == 4
        return {"documents": [["old", "new", "second", "other"] for _ in query_embeddings],
                "metadatas": [self.rows for _ in query_embeddings],
                "distances": [[0.01, 0.5, 0.2, 0.3] for _ in query_embeddings]}


def test_latest_capture_filter_and_unique_game_ranking():
    results = service.search(Collection(), Encoder(), "long query", top_k=5)
    assert [row["game_id"] for row in results] == [2, 1]
    assert results[0]["score"] == pytest.approx(0.8)
    assert results[1]["text"] == "new"
    assert results[1]["date"] == "2026-10-22"


def test_history_uses_best_fragment_and_respects_limit():
    results = service.search(Collection(), Encoder(), "text", top_k=1, latest_only=False)
    assert len(results) == 1 and results[0]["text"] == "old"


@pytest.mark.parametrize("query", ["", "   ", "x" * 4001])
def test_invalid_query_is_rejected(query):
    with pytest.raises(ValueError):
        service.search(Collection(), Encoder(), query)


def test_wrong_model_and_metric_are_rejected():
    collection = Collection()
    collection.metadata = {"config_sha256": "different"}
    with pytest.raises(ValueError, match="model"):
        service.search(collection, Encoder(), "text")
    collection.metadata = Collection.metadata
    collection.configuration = {"hnsw": {"space": "l2"}}
    with pytest.raises(ValueError, match="distance"):
        service.search(collection, Encoder(), "text")


def test_empty_catalog_and_inventory():
    collection = Collection()
    assert service.inventory(collection) == {"vectors": 4, "games": 2, "captures": 3,
                                             "dates": ["2026-10-20", "2026-10-22"]}
    collection.rows = []
    assert service.search(collection, Encoder(), "text") == []


def test_missing_database_is_not_created(tmp_path):
    target = tmp_path / "missing"
    with pytest.raises(ValueError):
        service.connect(target)
    assert not target.exists()

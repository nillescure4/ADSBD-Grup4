"""Unit tests for CLAP text-to-audio queries and Trusted RAWG selection."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import Mock

import numpy as np
import pytest

from src.exploitation.audio import search_audio as mod
from src.exploitation.audio import embedding_audio as index_mod


STORAGE = {
    "buckets": {"trusted": "3.trusted-zone"},
    "trusted": {"texts_prefix": "text"},
}


class FakeMinio:
    def __init__(self):
        self.objects = {
            "text/rawg/collection_date=2026-10-20/game_details.parquet": b"old",
            "text/rawg/collection_date=2026-10-21/game_details.parquet": b"new",
            "text/rawg/quality/overview.json": b"{}",
        }
        self.read_keys = []

    def get_object(self, *, Bucket, Key):
        assert Bucket == "3.trusted-zone"
        self.read_keys.append(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture
def fake_parquet(monkeypatch):
    data = {
        b"old": [
            {"id": 1, "name": "Old game", "description_clean": "An old adventure."},
        ],
        b"new": [
            {"id": 3, "name": "New game", "description_clean": "A dark fantasy adventure."},
            {"id": 4, "name": "No text", "description_clean": "  "},
            {"id": 5, "name": "Second game", "description_clean": "A fast racing game."},
        ],
    }

    class ParquetFile:
        def __init__(self, body):
            self.records = data[body]
            self.schema_arrow = SimpleNamespace(names=list(self.records[0]))

        def read(self, *, columns):
            return SimpleNamespace(to_pylist=lambda: [
                {col: item[col] for col in columns} for item in self.records
            ])

    fake_pa = ModuleType("pyarrow")
    fake_pq = ModuleType("pyarrow.parquet")
    fake_pa.BufferReader = lambda body: body
    fake_pq.ParquetFile = ParquetFile
    fake_pa.parquet = fake_pq
    fake_pa.__path__ = []
    monkeypatch.setitem(sys.modules, "pyarrow", fake_pa)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", fake_pq)
    monkeypatch.setattr(mod, "list_object_keys", lambda client, bucket, prefix: list(client.objects))
    return data


def test_trusted_latest_partition_and_nonempty_clean_descriptions(fake_parquet):
    minio = FakeMinio()
    records = mod.load_trusted_descriptions(minio, STORAGE)
    assert [item.game_id for item in records] == ["3", "5"]
    assert [item.description for item in records] == [
        "A dark fantasy adventure.", "A fast racing game.",
    ]
    assert records[0].collection_date == "2026-10-21"
    assert minio.read_keys == ["text/rawg/collection_date=2026-10-21/game_details.parquet"]


def test_trusted_can_select_specific_partition(fake_parquet):
    minio = FakeMinio()
    records = mod.load_trusted_descriptions(minio, STORAGE, "2026-10-20")
    assert len(records) == 1 and records[0].game_id == "1"
    assert len(minio.read_keys) == 1 and "2026-10-20" in minio.read_keys[0]


def test_trusted_rejects_missing_configuration_and_bad_date(fake_parquet):
    with pytest.raises(ValueError, match="trusted.texts_prefix"):
        mod.trusted_description_keys(FakeMinio(), {"trusted": {}, "buckets": STORAGE["buckets"]})
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        mod.load_trusted_descriptions(FakeMinio(), STORAGE, "2026/10/21")
    with pytest.raises(ValueError, match="No Trusted RAWG"):
        mod.load_trusted_descriptions(FakeMinio(), STORAGE, "2026-10-31")


def test_invalid_index_and_blank_text_rejected(fake_parquet):
    records = mod.load_trusted_descriptions(FakeMinio(), STORAGE)
    with pytest.raises(ValueError, match="between"):
        mod.query_by_text_position(records, 10)
    with pytest.raises(ValueError, match="empty"):
        mod.query_by_text(" \n ")


class FakeEncoder:
    def __init__(self):
        self.texts = []

    def embed_text(self, text):
        self.texts.append(text)
        return [0.6, 0.8]


def test_text_query_uses_rawg_description_exactly(fake_parquet):
    records = mod.load_trusted_descriptions(FakeMinio(), STORAGE)
    encoder = FakeEncoder()
    vector, selected = mod.query_by_text_position(records, 1, lambda: encoder)
    assert selected.game_id == "3"
    assert encoder.texts == ["A dark fantasy adventure."]
    assert vector == pytest.approx([0.6, 0.8])


def test_clap_text_encoder_normalizes_and_truncates_deterministically():
    import torch

    tokenizer = Mock(return_value={"input_ids": torch.ones((1, 512), dtype=torch.int64),
                                   "attention_mask": torch.ones((1, 512), dtype=torch.int64)})
    model = SimpleNamespace(get_text_features=Mock(return_value=torch.tensor([[3.0, 4.0]])))
    encoder = mod.ClapTextEncoder.__new__(mod.ClapTextEncoder)
    encoder.torch = torch
    encoder.device = "cpu"
    encoder.model = model
    encoder.processor = SimpleNamespace(tokenizer=tokenizer)
    assert encoder.embed_text("A fantasy adventure") == pytest.approx([0.6, 0.8])
    assert tokenizer.call_args.kwargs["max_length"] == 512
    assert tokenizer.call_args.kwargs["truncation"] is True
    model.get_text_features.assert_called_once()


class FakeCollection:
    def count(self):
        return 2

    def query(self, **kwargs):
        self.query_args = kwargs
        result = []
        for key in ("audio/a.flac", "audio/b.flac"):
            metadata = {
                "model": index_mod.MODEL_ID, "index_version": index_mod.INDEX_VERSION,
                "window_strategy": "center", "source_bucket": "3.trusted-zone",
                "source_key": key, "start_seconds": 5.0, "end_seconds": 15.0,
                "duration_seconds": 20.0,
            }
            result.append((index_mod.song_id(key), metadata))
        return {"ids": [[item[0] for item in result]],
                "metadatas": [[item[1] for item in result]],
                "distances": [[0.25, 0.38]]}


def test_text_similarity_query_compares_against_audio_collection_without_self_filter():
    collection = FakeCollection()
    matches = mod.nearest_songs(collection, [0.6, 0.8], "", 2)
    assert len(matches) == 2
    assert "where" not in collection.query_args
    assert collection.query_args["query_embeddings"][0] == pytest.approx([0.6, 0.8])
    assert matches[0]["source_key"] == "audio/a.flac"


def test_cli_literal_text_does_not_require_minio(monkeypatch, capsys):
    monkeypatch.setattr(mod, "open_existing_collection", lambda path: FakeCollection())
    monkeypatch.setattr(mod, "query_by_text", lambda text, encoder_factory=None: [0.6, 0.8])
    monkeypatch.setattr(mod, "create_minio_client", lambda: pytest.fail("MinIO not required"))
    monkeypatch.setattr(sys, "argv", [
        "search_audio", "--text", "A medieval adventure", "--top-k", "2",
    ])
    assert mod.main() == 0
    assert "audio/a.flac" in capsys.readouterr().out


def test_cli_trusted_text_from_minio(monkeypatch, capsys, fake_parquet):
    monkeypatch.setattr(mod, "open_existing_collection", lambda path: FakeCollection())
    monkeypatch.setattr(mod, "load_storage_config", lambda: STORAGE)
    monkeypatch.setattr(mod, "create_minio_client", FakeMinio)
    monkeypatch.setattr(mod, "query_by_text", lambda text, encoder_factory=None: [0.6, 0.8])
    monkeypatch.setattr(sys, "argv", ["search_audio", "--list-text", "--limit", "2"])
    assert mod.main() == 0
    assert "game_id=3" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["search_audio", "--text-index", "1", "--top-k", "2"])
    assert mod.main() == 0
    assert "audio/a.flac" in capsys.readouterr().out

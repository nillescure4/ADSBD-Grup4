"""Unit tests for one-vector-per-song CLAP similarity search."""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from src.exploitation.audio import embedding_audio as index_mod
from src.exploitation.audio import search_audio as mod


class FakeCollection:
    def __init__(self, songs=None):
        self.records = {}
        self.query_calls = []
        self.metadata = {"model": index_mod.MODEL_ID, "index_version": index_mod.INDEX_VERSION}
        self.configuration = {"hnsw": {"space": "cosine"}}
        vectors = {
            "audio/a.flac": [1.0, 0.0],
            "audio/b.flac": [0.8, 0.6],
            "audio/c.flac": [0.0, 1.0],
            "audio/d.flac": [-1.0, 0.0],
        }
        for key in (songs if songs is not None else list(vectors)):
            signature = index_mod.revision_signature(
                "3.trusted-zone", key, {"ETag": '"abc"', "ContentLength": 4},
            )
            excerpt = index_mod.select_center_excerpt(20.0)
            meta = index_mod.metadata_for(
                "3.trusted-zone", key, signature, excerpt, 20.0, "test-revision",
            )
            self.records[index_mod.song_id(key)] = (vectors.get(key, [0.6, 0.8]), meta)

    def count(self):
        return len(self.records)

    def get(self, *, ids=None, include, limit=None, offset=0):
        found = (list(self.records) if ids is None else
                 [key for key in ids if key in self.records])
        found = found[offset:offset + limit] if limit is not None else found
        result = {"ids": found}
        if "metadatas" in include:
            result["metadatas"] = [self.records[key][1] for key in found]
        if "embeddings" in include:
            result["embeddings"] = np.asarray([self.records[key][0] for key in found])
        return result

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        query = np.asarray(kwargs["query_embeddings"][0])
        matches = []
        for vid, (vec, meta) in self.records.items():
            if kwargs.get("where") and meta["source_key"] == kwargs["where"]["source_key"]["$ne"]:
                continue
            vec = np.asarray(vec)
            distance = 1 - float((query @ vec) / (np.linalg.norm(query) * np.linalg.norm(vec)))
            matches.append((distance, vid, meta))
        matches.sort(key=lambda row: (row[0], row[1]))
        matches = matches[:kwargs["n_results"]]
        return {
            "ids": [[row[1] for row in matches]],
            "metadatas": [[row[2] for row in matches]],
            "distances": [[row[0] for row in matches]],
        }


class FakeS3:
    def __init__(self):
        self.downloads = 0
        self.etag = '"abc"'

    def head_object(self, *, Bucket, Key):
        return {"ETag": self.etag, "ContentLength": 4}

    def download_file(self, bucket, key, destination):
        self.downloads += 1
        Path(destination).write_bytes(b"fLaC")


class FakeEncoder:
    def embed_audio(self, samples):
        return [0.6, 0.8]


def test_sorted_list_one_entry_per_song():
    songs = mod.indexed_songs(FakeCollection())
    assert [song.source_key for song in songs] == [
        "audio/a.flac", "audio/b.flac", "audio/c.flac", "audio/d.flac",
    ]
    assert songs[0].start_seconds == 5.0
    assert songs[0].end_seconds == 15.0


def test_indexed_query_reuses_vector():
    vector, key = mod.query_by_position(FakeCollection(), 1)
    assert key == "audio/a.flac" and vector == [1.0, 0.0]
    with pytest.raises(ValueError, match="between"):
        mod.query_by_position(FakeCollection(), 99)


def test_nearest_song_uses_chroma_cosine_and_excludes_self():
    collection = FakeCollection()
    hits = mod.nearest_songs(collection, [1, 0], "audio/a.flac", 3)
    assert [hit["source_key"] for hit in hits] == [
        "audio/b.flac", "audio/c.flac", "audio/d.flac",
    ]
    assert collection.query_calls[0]["where"] == {"source_key": {"$ne": "audio/a.flac"}}
    assert len({hit["source_key"] for hit in hits}) == 3


def test_include_self_returns_reference_song():
    hits = mod.nearest_songs(FakeCollection(), [1, 0], "audio/a.flac", 1,
                             include_self=True)
    assert hits[0]["source_key"] == "audio/a.flac"


def test_only_one_song_has_no_neighbors():
    result = mod.nearest_songs(FakeCollection(["audio/a.flac"]), [1, 0],
                               "audio/a.flac", 3)
    assert result == []


def test_query_from_indexed_minio_source_does_not_download():
    collection, s3 = FakeCollection(), FakeS3()
    vector, key = mod.query_by_source(collection, "audio/a.flac", s3,
                                      "3.trusted-zone")
    assert key == "audio/a.flac" and vector == [1.0, 0.0]
    assert s3.downloads == 0


def test_stale_indexed_query_source_is_rejected():
    s3 = FakeS3()
    s3.etag = '"new"'
    with pytest.raises(RuntimeError, match="stale"):
        mod.query_by_source(FakeCollection(), "audio/a.flac", s3, "3.trusted-zone")


def test_nonindexed_query_uses_same_center_window_as_indexer():
    s3 = FakeS3()
    with patch.object(mod, "probe_duration", return_value=75.0), \
         patch.object(mod, "decode_excerpt", return_value=np.ones(50, dtype=np.float32)) as decode, \
         patch.object(mod.shutil, "which", return_value="ffmpeg"):
        vector, source = mod.query_by_source(
            FakeCollection(), "audio/new.flac", s3, "3.trusted-zone",
            encoder_factory=FakeEncoder,
        )
    assert source == "audio/new.flac"
    assert vector == pytest.approx([0.6, 0.8])
    assert decode.call_args.args[1] == index_mod.Excerpt(32.5, 42.5)
    assert s3.downloads == 1


def test_invalid_query_and_invalid_vector_rejected():
    with pytest.raises(ValueError):
        mod.query_by_source(FakeCollection(), "../evil.flac", FakeS3(), "3.trusted-zone")
    with pytest.raises(ValueError):
        mod.checked_vector([float("nan"), 0])
    with pytest.raises(ValueError):
        mod.nearest_songs(FakeCollection(), [1, 0], "audio/a.flac", 0)


def test_open_collection_checks_version_and_does_not_create(tmp_path):
    target = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        mod.open_existing_collection(target)
    assert not target.exists()
    target.mkdir()
    (target / "chroma.sqlite3").touch()
    db = FakeCollection()
    fake_chroma = SimpleNamespace(
        PersistentClient=lambda path: SimpleNamespace(get_collection=lambda **kwargs: db),
    )
    with patch.dict(sys.modules, {"chromadb": fake_chroma}):
        assert mod.open_existing_collection(target).collection is db
        db.metadata["index_version"] = "old-version"
        with pytest.raises(RuntimeError, match="different model"):
            mod.open_existing_collection(target)


def test_report_lists_distinct_songs(tmp_path):
    results = mod.nearest_songs(FakeCollection(), [1, 0], "audio/a.flac", 2)
    target = tmp_path / "nearest.csv"
    mod.save_results(target, results)
    with target.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 2
    assert rows[0]["source_key"] == "audio/b.flac"
    assert rows[0]["start_seconds"] == "5.0"


def test_cli_list_and_search_read_only(monkeypatch, capsys, tmp_path):
    db = FakeCollection()
    monkeypatch.setattr(mod, "open_existing_collection", lambda path: db)
    monkeypatch.setattr(sys, "argv", ["search_audio", "--list", "--limit", "2"])
    assert mod.main() == 0
    output = capsys.readouterr().out
    assert "[1] audio/a.flac" in output
    assert "[2] audio/b.flac" in output
    report = tmp_path / "nearest.csv"
    monkeypatch.setattr(sys, "argv", [
        "search_audio", "--query-index", "1", "--top-k", "2",
        "--report", str(report),
    ])
    assert mod.main() == 0
    output = capsys.readouterr().out
    assert "audio/b.flac" in output
    assert "audio/a.flac" not in output
    assert report.is_file() and db.count() == 4

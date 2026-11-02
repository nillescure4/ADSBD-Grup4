"""Unit tests for centered, one-vector-per-song CLAP indexing."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from src.exploitation.audio import embedding_audio as mod


class FakeS3:
    def __init__(self, data: bytes = b"fLaCfake"):
        self.data = data
        self.downloads = 0
        self.etag = '"abc"'

    def head_object(self, *, Bucket, Key):
        return {"ETag": self.etag, "ContentLength": len(self.data)}

    def download_file(self, bucket, key, filename):
        self.downloads += 1
        Path(filename).write_bytes(self.data)


class FakeCollection:
    def __init__(self):
        self.rows = {}

    def get(self, *, ids, include):
        matched = [key for key in ids if key in self.rows]
        return {"ids": matched, "metadatas": [self.rows[key][1] for key in matched]}

    def add(self, *, ids, embeddings, metadatas):
        for key, embedding, metadata in zip(ids, embeddings, metadatas):
            if key in self.rows:
                raise AssertionError("Duplicate vector added")
            self.rows[key] = (embedding, metadata)

    def count(self):
        return len(self.rows)


class FakeEncoder:
    model_revision = "test-model-revision"

    def embed_audio(self, samples):
        assert samples.dtype == np.float32
        return [0.6, 0.8]


def test_select_audio_keys_only_flacs():
    keys = ["audio/b.flac", "image/a.flac", "audio/c.wav", "audio/a.FLAC"]
    assert mod.select_audio_keys(keys, 1) == ["audio/a.FLAC"]
    assert mod.select_audio_keys(keys, None) == ["audio/a.FLAC", "audio/b.flac"]
    with pytest.raises(ValueError):
        mod.select_audio_keys(keys, 0)


@pytest.mark.parametrize("duration,start,end", [
    (3.0, 0.0, 3.0), (10.0, 0.0, 10.0),
    (12.0, 1.0, 11.0), (20.0, 5.0, 15.0),
    (35.0, 12.5, 22.5), (120.0, 55.0, 65.0),
])
def test_centered_window(duration, start, end):
    excerpt = mod.select_center_excerpt(duration)
    assert excerpt.start == pytest.approx(start)
    assert excerpt.end == pytest.approx(end)
    assert duration - excerpt.end == pytest.approx(excerpt.start)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_invalid_durations_rejected(duration):
    with pytest.raises(ValueError):
        mod.select_center_excerpt(duration)


def test_song_ids_stable_unique_and_policy_isolated():
    assert mod.song_id("audio/a.flac") == mod.song_id("audio/a.flac")
    assert mod.song_id("audio/a.flac") != mod.song_id("audio/b.flac")
    assert mod.COLLECTION_NAME != "audio_clap_v1"
    assert "center" in mod.INDEX_VERSION


def test_revision_signature_checks_source():
    head = {"ETag": '"abc"', "ContentLength": 111,
            "LastModified": datetime(2026, 10, 20, tzinfo=timezone.utc)}
    sig = mod.revision_signature("trusted", "audio/a.flac", head)
    assert sig != mod.revision_signature("trusted", "audio/a.flac",
                                         {**head, "ETag": '"def"'})


def test_process_indexes_centered_song_then_skips_without_downloading_again():
    source, collection, encoder, rows = FakeS3(), FakeCollection(), FakeEncoder(), []
    with patch.object(mod, "probe_duration", return_value=100.0), \
         patch.object(mod, "decode_excerpt", return_value=np.ones(48, dtype=np.float32)) as decode:
        args = (source, collection, encoder, "3.trusted-zone", "audio/a.flac", rows.append)
        # Capture arguments with a helper preserving the writer call signature.
        def writer(*fields):
            rows.append(fields)
        args = (*args[:-1], writer)
        assert mod.process_file(*args) == {"indexed": 1, "skipped": 0, "failed": 0}
        assert mod.process_file(*args) == {"indexed": 0, "skipped": 1, "failed": 0}
    assert source.downloads == 1
    assert decode.call_args.args[1] == mod.Excerpt(45.0, 55.0)
    assert collection.count() == 1
    meta = collection.rows[mod.song_id("audio/a.flac")][1]
    assert meta["start_seconds"] == 45.0
    assert meta["end_seconds"] == 55.0
    assert meta["duration_seconds"] == 100.0
    assert meta["window_strategy"] == "center"
    assert [row[0] for row in rows] == ["indexed", "skipped"]


def test_changed_source_is_rejected_without_overwrite():
    source, collection, rows = FakeS3(), FakeCollection(), []
    with patch.object(mod, "probe_duration", return_value=8.0), \
         patch.object(mod, "decode_excerpt", return_value=np.ones(48, dtype=np.float32)):
        args = (source, collection, FakeEncoder(), "3.trusted-zone",
                "audio/a.flac", lambda *row: rows.append(row))
        assert mod.process_file(*args)["indexed"] == 1
        source.etag = '"new"'
        result = mod.process_file(*args)
    assert result == {"indexed": 0, "skipped": 0, "failed": 1}
    assert collection.count() == 1
    assert source.downloads == 1


def test_dry_run_does_not_load_clap_or_touch_chroma(tmp_path):
    with patch.object(mod, "list_object_keys", return_value=["audio/a.flac", "audio/b.flac"]):
        result = mod.run_pipeline(
            FakeS3(), {"buckets": {"trusted": "3.trusted-zone"}},
            limit=1, dry_run=True, chroma_path=tmp_path / "chroma",
            report=tmp_path / "report.csv",
            encoder_factory=lambda: pytest.fail("Loaded model"),
            collection_factory=lambda path: pytest.fail("Created collection"),
        )
    assert result["planned"] == 1 and result["selected"] == 1
    assert not (tmp_path / "chroma").exists()


def test_real_ffmpeg_decodes_middle_ten_seconds(tmp_path):
    if not mod.shutil.which("ffmpeg") or not mod.shutil.which("ffprobe"):
        pytest.skip("FFmpeg unavailable")
    source = tmp_path / "tone.wav"
    proc = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=16", "-y", str(source),
    ], capture_output=True, check=False)
    assert proc.returncode == 0
    duration = mod.probe_duration(source)
    excerpt = mod.select_center_excerpt(duration)
    assert excerpt.start == pytest.approx(3.0, abs=0.01)
    assert excerpt.end == pytest.approx(13.0, abs=0.01)
    samples = mod.decode_excerpt(source, excerpt)
    assert samples.dtype == np.float32
    assert abs(samples.size - 480_000) <= 5

"""Unit tests for audio Temporal Landing ingestion."""

from pathlib import Path

import pytest

from src.ingestion.audio import audio_landing


class FakeS3Client:
    """Minimal in-memory S3 client for testing upload and HEAD requests."""

    def __init__(self):
        self.objects = {}
        self.upload_calls = 0

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.upload_calls += 1
        self.objects[(bucket, key)] = {
            "data": Path(filename).read_bytes(),
            "Metadata": (ExtraArgs or {}).get("Metadata", {}),
        }

    def head_object(self, *, Bucket, Key):
        obj = self.objects[(Bucket, Key)]
        return {"ContentLength": len(obj["data"]), "Metadata": obj["Metadata"]}


def write_audio(tmp_path, subdir, filename, data=b"fake audio bytes"):
    """Create a fake audio asset without requiring an audio decoder."""
    directory = tmp_path / subdir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(data)
    return path


def test_discovery_preserves_submissions_and_ignores_non_audio(tmp_path):
    write_audio(tmp_path, "100213", "Loop_Rejoicing.wav")
    write_audio(tmp_path, "100180", "Synth%20Wave_0.MP3")
    (tmp_path / "notes.jsonl").write_text("{}", encoding="utf-8")
    files = audio_landing.discover_audio_files(tmp_path)
    assert [file.relative_to(tmp_path).as_posix() for file in files] == [
        "100180/Synth%20Wave_0.MP3",
        "100213/Loop_Rejoicing.wav",
    ]


def test_temporal_key_keeps_original_filename(tmp_path):
    path = write_audio(tmp_path, "100180", "Synth%20Wave_0.mp3")
    assert audio_landing.temporal_object_key(tmp_path, path, "temporal") == (
        "temporal/audio/100180/Synth%20Wave_0.mp3"
    )


def test_dry_run_never_contacts_minio(tmp_path, monkeypatch):
    write_audio(tmp_path, "100180", "track.mp3")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("MinIO must not be consulted during a dry run")

    monkeypatch.setattr(audio_landing, "list_object_keys", fail_if_called)
    stats = audio_landing.ingest_audio(
        tmp_path, client=None, bucket="landing", temporal_prefix="temporal", dry_run=True
    )
    assert (stats.planned, stats.uploaded, stats.failed) == (1, 0, 0)


def test_upload_stores_checksum_and_size(tmp_path, monkeypatch):
    path = write_audio(tmp_path, "100213", "Rejoicing_0.mp3", b"abcdef")
    client = FakeS3Client()
    monkeypatch.setattr(audio_landing, "list_object_keys", lambda *args: [])

    stats = audio_landing.ingest_audio(
        tmp_path, client=client, bucket="landing", temporal_prefix="temporal"
    )
    key = audio_landing.temporal_object_key(tmp_path, path, "temporal")
    assert stats.uploaded == 1
    assert client.head_object(Bucket="landing", Key=key) == {
        "ContentLength": 6,
        "Metadata": {"sha256": audio_landing.sha256_file(path)},
    }


def test_repeat_run_skips_identical_file(tmp_path, monkeypatch):
    write_audio(tmp_path, "100213", "track.mp3")
    client = FakeS3Client()
    monkeypatch.setattr(
        audio_landing,
        "list_object_keys",
        lambda *args: [key for bucket, key in client.objects],
    )

    first = audio_landing.ingest_audio(
        tmp_path, client=client, bucket="landing", temporal_prefix="temporal"
    )
    second = audio_landing.ingest_audio(
        tmp_path, client=client, bucket="landing", temporal_prefix="temporal"
    )
    assert (first.uploaded, second.skipped, client.upload_calls) == (1, 1, 1)


def test_changed_file_is_rejected_without_overwrite(tmp_path, monkeypatch):
    path = write_audio(tmp_path, "100213", "track.mp3", b"original")
    client = FakeS3Client()
    monkeypatch.setattr(
        audio_landing,
        "list_object_keys",
        lambda *args: [key for bucket, key in client.objects],
    )
    audio_landing.ingest_audio(tmp_path, client=client, bucket="landing", temporal_prefix="temporal")
    path.write_bytes(b"modified")
    stats = audio_landing.ingest_audio(
        tmp_path, client=client, bucket="landing", temporal_prefix="temporal"
    )
    assert (stats.failed, stats.uploaded, client.upload_calls) == (1, 0, 1)


def test_default_limit_selects_only_first_five(tmp_path, monkeypatch):
    for index in range(10):
        write_audio(tmp_path, str(index), f"song_{index}.ogg")
    stats = audio_landing.ingest_audio(
        tmp_path, client=None, bucket="landing", temporal_prefix="temporal", dry_run=True
    )
    assert (stats.found, stats.selected, stats.planned) == (10, 5, 5)


def test_empty_audio_is_not_uploaded(tmp_path, monkeypatch):
    write_audio(tmp_path, "100180", "empty.wav", data=b"")
    client = FakeS3Client()
    monkeypatch.setattr(audio_landing, "list_object_keys", lambda *args: [])
    stats = audio_landing.ingest_audio(
        tmp_path, client=client, bucket="landing", temporal_prefix="temporal"
    )
    assert (stats.failed, client.upload_calls) == (1, 0)


def test_missing_folder_raises(tmp_path):
    with pytest.raises(NotADirectoryError):
        audio_landing.discover_audio_files(tmp_path / "missing")


def test_object_listing_uses_audio_prefix(tmp_path, monkeypatch):
    """Search only within the dedicated audio prefix in Temporal Landing."""
    write_audio(tmp_path, "100180", "track.mp3")
    observed = []

    def record_listing(client, bucket, prefix):
        observed.append((bucket, prefix))
        return []

    monkeypatch.setattr(audio_landing, "list_object_keys", record_listing)
    audio_landing.ingest_audio(
        tmp_path, client=FakeS3Client(), bucket="landing", temporal_prefix="temporal"
    )
    assert observed == [("landing", "temporal/audio/")]

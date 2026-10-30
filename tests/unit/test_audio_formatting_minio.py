"""Unit tests for MinIO-based Audio Formatted orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from src.formatting.audio import formatting_audio as audio


STORAGE = {
    "buckets": {"landing": "1.landing-zone", "formatted": "2.formatted-zone"},
    "landing": {"persistent_prefix": "persistent"},
}
SOURCE_KEY = "persistent/audio/opengameart$music$18-10-2026$100180$Synth%20Wave_0.mp3"
DEST_KEY = "audio/opengameart$music$18-10-2026$100180$Synth%20Wave_0.mp3.flac"


def missing_object() -> ClientError:
    """Produce an S3-compatible 404 without accessing MinIO."""
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


class FakeMinioClient:
    """Store a minimal in-memory view of the two S3 buckets."""

    def __init__(self) -> None:
        self.objects = {
            ("1.landing-zone", SOURCE_KEY): {
                "content": b"example-audio-bytes",
                "metadata": {},
                "modified": datetime(2026, 10, 16, tzinfo=timezone.utc),
            }
        }
        self.uploads = 0
        self.downloads = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        obj = self.objects.get((Bucket, Key))
        if obj is None:
            raise missing_object()
        return {
            "ETag": '"test-etag-' + str(len(obj["content"])) + '"',
            "ContentLength": len(obj["content"]),
            "LastModified": obj["modified"],
            "Metadata": obj["metadata"],
        }

    def download_file(self, bucket: str, key: str, file_path: str) -> None:
        self.downloads += 1
        Path(file_path).write_bytes(self.objects[(bucket, key)]["content"])

    def upload_file(self, file_path: str, bucket: str, key: str, ExtraArgs: dict) -> None:
        self.uploads += 1
        self.objects[(bucket, key)] = {
            "content": Path(file_path).read_bytes(),
            "metadata": ExtraArgs["Metadata"],
            "modified": datetime(2026, 10, 16, tzinfo=timezone.utc),
        }


def test_source_prefix_is_restricted_to_persistent() -> None:
    """Do not accidentally process temporary landing or unrelated buckets."""
    assert audio.normalize_source_prefix("persistent/audio", "persistent") == "persistent/audio/"
    with pytest.raises(ValueError):
        audio.normalize_source_prefix("temporal/audio", "persistent")
    with pytest.raises(ValueError):
        audio.normalize_source_prefix("persistent", "persistent")


def test_formatted_key_mirrors_hierarchy_without_colliding() -> None:
    """Preserve hierarchy and original extensions as part of output filenames."""
    assert audio.destination_key_for(SOURCE_KEY, "persistent") == DEST_KEY
    assert audio.destination_key_for(SOURCE_KEY[:-3] + "wav", "persistent") != DEST_KEY


def test_select_only_audio_objects_under_prefix() -> None:
    keys = [
        SOURCE_KEY,
        "persistent/audio/files.jsonl",
        "persistent/text/another.mp3",
        "persistent/audio/cover.jpg",
    ]
    assert audio.plan_audio_objects(keys, "persistent/audio/", None) == [SOURCE_KEY]


def test_empty_landing_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pipeline should be prepared before the team loads any source audio."""
    monkeypatch.setattr(audio, "list_object_keys", lambda *_: [])
    results = audio.run_formatting(FakeMinioClient(), STORAGE, "persistent/audio/")
    assert results["selected"] == 0
    assert results["failed"] == 0


def test_dry_run_does_not_download_or_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeMinioClient()
    monkeypatch.setattr(audio, "list_object_keys", lambda *_: [SOURCE_KEY])
    results = audio.run_formatting(client, STORAGE, "persistent/audio/", dry_run=True)
    assert results["planned"] == 1
    assert client.downloads == 0
    assert client.uploads == 0


def test_convert_publish_and_incremental_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A rerun must skip current outputs and preserve the original object."""
    client = FakeMinioClient()
    monkeypatch.setattr(audio, "list_object_keys", lambda *_: [SOURCE_KEY])
    monkeypatch.setattr(audio, "require_tools", lambda: None)

    def fake_convert(src: Path, dst: Path) -> tuple[str, audio.AudioInfo, int, str]:
        assert src.exists()
        dst.write_bytes(b"flac-placeholder")
        return "converted", audio.AudioInfo("mp3", 44100, 2, None, "fltp", 90.0), 16, ""

    monkeypatch.setattr(audio, "convert_audio", fake_convert)
    first = audio.run_formatting(
        client, STORAGE, "persistent/audio/", report=tmp_path / "first.csv"
    )
    assert first["converted"] == 1
    assert client.downloads == 1
    assert client.uploads == 1
    assert ("1.landing-zone", SOURCE_KEY) in client.objects
    assert ("2.formatted-zone", DEST_KEY) in client.objects
    second = audio.run_formatting(
        client, STORAGE, "persistent/audio/", report=tmp_path / "second.csv"
    )
    assert second["skipped"] == 1
    assert client.downloads == 1
    assert client.uploads == 1


def test_existing_untracked_output_is_not_overwritten(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = FakeMinioClient()
    client.objects[("2.formatted-zone", DEST_KEY)] = {
        "content": b"older-result", "metadata": {},
        "modified": datetime(2026, 10, 16, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(audio, "list_object_keys", lambda *_: [SOURCE_KEY])
    monkeypatch.setattr(audio, "require_tools", lambda: None)
    outcome = audio.run_formatting(client, STORAGE, "persistent/audio/", report=tmp_path / "report.csv")
    assert outcome["failed"] == 1
    assert client.downloads == 0
    assert client.uploads == 0


def test_cli_defaults_to_configured_persistent_audio_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the configured persistent area and avoid requiring outdated source paths."""
    import sys

    observed = {}
    def fake_run(client, storage, prefix, **kwargs):
        observed["prefix"] = prefix
        return {"selected": 0, "converted": 0, "skipped": 0, "unsupported": 0, "failed": 0, "planned": 0}

    monkeypatch.setattr(sys, "argv", ["audio", "--dry-run"])
    monkeypatch.setattr(audio, "load_storage_config", lambda: STORAGE)
    monkeypatch.setattr(audio, "create_minio_client", lambda: object())
    monkeypatch.setattr(audio, "run_formatting", fake_run)
    assert audio.main() == 0
    assert observed["prefix"] == "persistent/audio/"

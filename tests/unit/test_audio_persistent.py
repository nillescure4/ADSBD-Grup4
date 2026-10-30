"""Unit tests for verified audio promotion and rerun safety."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.ingestion.audio import audio_landing, audio_persistent


class StreamingBody:
    """Provide an S3-like streaming body for checksum tests."""

    def __init__(self, data):
        self.data = data

    def iter_chunks(self, chunk_size):
        for offset in range(0, len(self.data), chunk_size):
            yield self.data[offset:offset + chunk_size]

    def close(self):
        pass


class FakeS3:
    """Store object bytes, SHA256 metadata, and deterministic upload times."""

    def __init__(self):
        self.objects = {}
        self.upload_calls = 0
        self.copy_calls = 0
        self.delete_calls = 0
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        self.corrupt_copies = False

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.upload_calls += 1
        self.objects[(bucket, key)] = {
            "data": Path(filename).read_bytes(),
            "Metadata": (ExtraArgs or {}).get("Metadata", {}),
            "LastModified": self.now,
        }

    def head_object(self, *, Bucket, Key):
        obj = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(obj["data"]),
            "Metadata": dict(obj["Metadata"]),
            "LastModified": obj["LastModified"],
        }

    def get_object(self, *, Bucket, Key):
        return {"Body": StreamingBody(self.objects[(Bucket, Key)]["data"])}

    def copy_object(self, *, Bucket, Key, CopySource):
        self.copy_calls += 1
        source = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        self.objects[(Bucket, Key)] = {
            "data": b"corrupted" if self.corrupt_copies else source["data"],
            "Metadata": dict(source["Metadata"]),
            "LastModified": self.now,
        }

    def delete_object(self, *, Bucket, Key):
        self.delete_calls += 1
        del self.objects[(Bucket, Key)]


def setup_audio(tmp_path, monkeypatch, data=b"audio bytes", filename="track.mp3"):
    directory = tmp_path / "100180"
    directory.mkdir()
    path = directory / filename
    path.write_bytes(data)
    client = FakeS3()

    def list_keys(client_obj, bucket, prefix=""):
        return sorted(k for b, k in client_obj.objects if b == bucket and k.startswith(prefix))

    monkeypatch.setattr(audio_landing, "list_object_keys", list_keys)
    monkeypatch.setattr(audio_persistent, "list_object_keys", list_keys)
    return client, path


def ingest(tmp_path, client):
    return audio_landing.ingest_audio(
        tmp_path, client=client, bucket="landing",
        temporal_prefix="temporal", persistent_prefix="persistent",
    )


def promote(client, dry_run=False):
    return audio_persistent.promote_audio(
        client, bucket="landing", temporal_prefix="temporal",
        persistent_prefix="persistent", dry_run=dry_run,
    )


def test_persistent_naming_is_flat_and_deterministic():
    key = audio_persistent.persistent_object_key(
        "temporal/audio/100180/Synth%20Wave_0.mp3", "temporal", "persistent",
        datetime(2026, 10, 18, 9, tzinfo=timezone.utc),
    )
    assert key == "persistent/audio/opengameart$music$18-10-2026$100180$Synth%20Wave_0.mp3"
    assert audio_persistent.persistent_identity(key, "persistent") == ("100180", "Synth%20Wave_0.mp3")


def test_promote_verifies_bytes_and_deletes_temporal(tmp_path, monkeypatch):
    client, path = setup_audio(tmp_path, monkeypatch)
    assert ingest(tmp_path, client).uploaded == 1
    result = promote(client)
    assert (result.promoted, result.failed, client.delete_calls) == (1, 0, 1)
    key = next(k for _, k in client.objects)
    assert key.startswith("persistent/audio/")
    assert client.objects[("landing", key)]["data"] == path.read_bytes()


def test_second_ingestion_skips_already_promoted_audio(tmp_path, monkeypatch):
    client, _ = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    promote(client)
    second = ingest(tmp_path, client)
    assert (second.uploaded, second.skipped, client.upload_calls) == (0, 1, 1)


def test_changed_file_does_not_overwrite_persistent(tmp_path, monkeypatch):
    client, path = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    promote(client)
    path.write_bytes(b"changed")
    repeated = ingest(tmp_path, client)
    assert (repeated.failed, client.upload_calls, client.delete_calls) == (1, 1, 1)


def test_corrupted_copy_keeps_temporal(tmp_path, monkeypatch):
    client, _ = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    client.corrupt_copies = True
    result = promote(client)
    assert (result.failed, result.promoted, client.delete_calls) == (1, 0, 0)
    assert ("landing", "temporal/audio/100180/track.mp3") in client.objects


def test_rerun_recovers_verified_existing_destination(tmp_path, monkeypatch):
    client, _ = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    source = "temporal/audio/100180/track.mp3"
    dest = audio_persistent.persistent_object_key(
        source, "temporal", "persistent", client.now
    )
    client.copy_object(Bucket="landing", Key=dest, CopySource={"Bucket": "landing", "Key": source})
    result = promote(client)
    assert (result.recovered, result.promoted, client.delete_calls, client.copy_calls) == (1, 0, 1, 1)


def test_collision_uses_first_persistent_date(tmp_path, monkeypatch):
    client, _ = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    promote(client)
    first_key = next(k for _, k in client.objects)
    original = client.objects[("landing", first_key)]
    client.now += timedelta(days=2)
    client.objects[("landing", "temporal/audio/100180/track.mp3")] = {
        "data": original["data"], "Metadata": original["Metadata"], "LastModified": client.now
    }
    result = promote(client)
    assert (result.recovered, client.copy_calls, len(client.objects)) == (1, 1, 1)
    assert first_key == next(k for _, k in client.objects)


def test_dry_run_does_not_copy_or_delete(tmp_path, monkeypatch):
    client, _ = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    stats = promote(client, dry_run=True)
    assert (stats.planned, stats.promoted, client.copy_calls, client.delete_calls) == (1, 0, 0, 0)


def test_missing_checksum_prevents_promotion(tmp_path, monkeypatch):
    client, _ = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    client.objects[("landing", "temporal/audio/100180/track.mp3")]["Metadata"] = {}
    stats = promote(client)
    assert (stats.failed, client.delete_calls) == (1, 0)


def test_ambiguous_persistent_identity_rejected(tmp_path, monkeypatch):
    client, _ = setup_audio(tmp_path, monkeypatch)
    ingest(tmp_path, client)
    promote(client)
    first_key = next(k for _, k in client.objects)
    client.objects[("landing", first_key.replace("18-10-2026", "19-10-2026"))] = (
        dict(client.objects[("landing", first_key)])
    )
    result = ingest(tmp_path, client)
    assert result.failed == 1

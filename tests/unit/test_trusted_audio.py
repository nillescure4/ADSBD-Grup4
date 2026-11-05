"""Tests for safe audio deduplication, DC decisions, and publication."""
from __future__ import annotations

import csv
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.trusted.audio.trusted_audio import (
    check_source_head, correct_dc, metadata_for, process_one, run_trusted,
    select_method, source_row_valid,
)
from src.trusted.audio.analyze_audio_quality import probe_flac, inspect_signal


def make_row(key="audio/a.flac", *, dc="0.0", peak="-5", flags="", duplicate=""):
    """Build minimal compatible Full Quality CSV rows."""
    return {
        "status": "review" if flags else "ok", "source_key": key,
        "source_etag": "etag123", "size_bytes": "1000", "sha256": "a" * 64,
        "duration_seconds": "2", "codec": "flac", "sample_rate_hz": "44100",
        "channels": "1", "bit_depth": "16", "peak_dbfs": peak,
        "rms_dbfs": "-16", "dc_offset": dc, "silence_seconds": "0",
        "silence_ratio": "0", "duplicate_of": duplicate, "flags": flags, "error": "",
    }


def test_copies_clean_audio_unchanged():
    assert select_method(make_row())[0] == "copy"


def test_detects_sha_duplicate_before_any_correction():
    method, _ = select_method(make_row(dc="0.1", peak="-20", flags="dc_offset;identical_file",
                                       duplicate="audio/original.flac"))
    assert method == "duplicate"


def test_corrects_dc_only_with_headroom():
    assert select_method(make_row(dc="0.06", peak="-12", flags="dc_offset"))[0] == "dcshift"
    method, message = select_method(make_row(dc="0.06", peak="-0.01", flags="dc_offset"))
    assert method == "copy"
    assert "headroom" in message


def test_rejects_invalid_source_key():
    with pytest.raises(ValueError):
        source_row_valid(make_row("audio/../outside.flac"))


def test_metadata_records_source_hash_and_policy():
    info = metadata_for(make_row(), "copy")
    assert info["source-sha256"] == "a" * 64
    assert info["trusted-policy"] == "audio-trusted-v1"


def test_rejects_stale_source_revision():
    client = SimpleNamespace(head_object=lambda **kwargs: {"ETag": '"different"', "ContentLength": 1000})
    with pytest.raises(RuntimeError, match="Source changed"):
        check_source_head(client, "2.formatted-zone", make_row())


def test_duplicates_are_not_copied():
    class Client:
        def head_object(self, **kwargs):
            return {"ETag": '"etag123"', "ContentLength": 1000}
        def copy_object(self, **kwargs):
            raise AssertionError("Duplicate must not be copied")
    row = make_row("audio/second.flac", flags="identical_file", duplicate="audio/a.flac")
    result = process_one(Client(), "formatted", "trusted", row)
    assert result["status"] == "duplicate_excluded"


@pytest.mark.parametrize("damaged", [False, True])
def test_refreshes_only_intact_tracked_derivatives(damaged):
    import hashlib
    from io import BytesIO
    old, new = b"old-flac", b"new-flac-content"
    row = make_row()
    row.update(sha256=hashlib.sha256(new).hexdigest(), size_bytes=str(len(new)))
    old_row = {**row, "sha256": hashlib.sha256(old).hexdigest()}
    class Client:
        body = b"damaged" if damaged else old
        metadata = metadata_for(old_row, "copy")
        copies = 0
        def head_object(self, Bucket, Key):
            return {"ETag": '"etag123"', "ContentLength": len(new) if Bucket == "formatted" else len(self.body),
                    "Metadata": {} if Bucket == "formatted" else self.metadata}
        def get_object(self, **kwargs):
            return {"Body": BytesIO(self.body)}
        def copy_object(self, **kwargs):
            self.copies += 1
            self.body, self.metadata = new, kwargs["Metadata"]
    client = Client()
    result = process_one(client, "formatted", "trusted", row)
    assert result["status"] == ("failed" if damaged else "copied")
    assert client.copies == (0 if damaged else 1)
    if not damaged:
        assert client.body == new
        assert process_one(client, "formatted", "trusted", row)["status"] == "skipped"


def test_runs_dryrun_without_writing(tmp_path: Path):
    rows = [make_row(), make_row("audio/b.flac", flags="identical_file", duplicate="audio/a.flac")]
    file = tmp_path / "quality.csv"
    with file.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    class Client:
        pass
    # Override list_object_keys at its import site.
    import src.trusted.audio.trusted_audio as module
    original = module.list_object_keys
    try:
        module.list_object_keys = lambda *_: ["audio/a.flac", "audio/b.flac"]
        counts = run_trusted(Client(), {"buckets": {"formatted": "formatted", "trusted": "trusted"}},
                             file, limit=2, dry_run=True)
        assert counts["planned"] == 2
        assert counts["duplicate_excluded"] == 0
    finally:
        module.list_object_keys = original


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg missing")
def test_real_dcshift_conserves_flac_properties_and_removes_offset(tmp_path: Path):
    """Use an actual stereo FLAC with known DC offset to validate the filter."""
    src_wav = tmp_path / "source.wav"
    src_flac = tmp_path / "source.flac"
    output = tmp_path / "clean.flac"
    sample_rate = 16000
    with wave.open(str(src_wav), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        frames = bytearray()
        for sample in range(sample_rate * 2):
            value = int((0.08 + 0.2 * math.sin(2 * math.pi * 220 * sample / sample_rate)) * 32767)
            frames += struct.pack("<hh", value, value)
        writer.writeframes(frames)
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i",
                    str(src_wav), "-c:a", "flac", str(src_flac)], check=True)
    properties = probe_flac(src_flac)
    metrics = inspect_signal(src_flac, properties["duration_seconds"])
    row = make_row(dc=str(metrics["dc_offset"]), peak=str(metrics["peak_dbfs"]), flags="dc_offset")
    assert select_method(row)[0] == "dcshift"
    after_dc, after_peak = correct_dc(src_flac, output, row)
    assert abs(after_dc) < 0.01
    assert after_peak < 0
    assert probe_flac(output)["sample_rate_hz"] == sample_rate
    assert probe_flac(output)["channels"] == 2

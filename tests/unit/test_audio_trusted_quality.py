"""Focused tests for the first, non-destructive Trusted audio quality step."""

from __future__ import annotations

import csv
import shutil
import subprocess
from pathlib import Path

import pytest

from src.trusted.audio import analyze_audio_quality as quality


def test_parse_metrics_uses_overall_instead_of_channel_stats():
    output = """
[Parsed_astats] Peak level dB: -9.0
[Parsed_astats] Overall
[Parsed_astats] DC offset: 0.000100
[Parsed_astats] Peak level dB: -6.000000
[Parsed_astats] RMS level dB: -19.000000
[Parsed_silencedetect] silence_end: 4.0 | silence_duration: 2.5
"""
    values = quality.parse_audio_metrics(output, duration=10)
    assert values["peak_dbfs"] == -6
    assert values["rms_dbfs"] == -19
    assert values["dc_offset"] == 0.0001
    assert values["silence_seconds"] == 2.5
    assert values["silence_ratio"] == 0.25


def test_missing_overall_is_invalid():
    with pytest.raises(ValueError, match="overall audio"):
        quality.parse_audio_metrics("Peak level dB: -4", duration=1)


def test_threshold_flags_are_review_only():
    row = {column: "" for column in quality.REPORT_COLUMNS}
    row.update(status="ok", duration_seconds="90", peak_dbfs="-0.01",
               dc_offset="0.03", silence_ratio="0.4")
    assert quality.classify(row) == "review"
    assert set(row["flags"].split(";")) == {
        "near_full_scale", "dc_offset", "prolonged_silence",
    }


def test_normal_audio_is_ok():
    row = {column: "" for column in quality.REPORT_COLUMNS}
    row.update(status="ok", duration_seconds="80", peak_dbfs="-3",
               dc_offset="0.0001", silence_ratio="0.04")
    assert quality.classify(row) == "ok"
    assert row["flags"] == ""


def test_exact_duplicate_is_flagged():
    row = {column: "" for column in quality.REPORT_COLUMNS}
    row.update(status="ok", duration_seconds="80", peak_dbfs="-3",
               dc_offset="0", silence_ratio="0", duplicate_of="audio/previous.flac")
    assert quality.classify(row) == "review"
    assert row["flags"] == "identical_file"


def test_dry_run_does_not_download(monkeypatch, tmp_path):
    class FakeClient:
        def download_file(self, *args):
            raise AssertionError("A dry-run should not download content")
    monkeypatch.setattr(quality, "list_object_keys", lambda *args: [
        "audio/a.flac", "audio/b.flac", "audio/manifest.csv",
    ])
    result = quality.run_analysis(FakeClient(), {"buckets": {"formatted": "2.formatted-zone"}},
                                  dry_run=True, limit=1, report=tmp_path / "report.csv")
    assert result == {"selected": 1, "ok": 0, "review": 0, "invalid": 0,
                      "failed": 0, "planned": 1}
    assert not (tmp_path / "report.csv").exists()


def test_report_is_generated_with_per_file_outcomes(monkeypatch, tmp_path):
    monkeypatch.setattr(quality, "list_object_keys", lambda *args: [
        "audio/b.flac", "audio/a.flac",
    ])
    monkeypatch.setattr(quality, "require_tools", lambda: None)
    def fake_profile(client, bucket, key, hashes):
        row = {column: "" for column in quality.REPORT_COLUMNS}
        row.update(source_key=key, status="ok" if key.endswith("a.flac") else "review")
        return row
    monkeypatch.setattr(quality, "profile_object", fake_profile)
    path = tmp_path / "quality.csv"
    result = quality.run_analysis(object(), {"buckets": {"formatted": "2.formatted-zone"}},
                                  report=path)
    assert result == {"selected": 2, "ok": 1, "review": 1, "invalid": 0,
                      "failed": 0, "planned": 0}
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["source_key"] for row in rows] == ["audio/a.flac", "audio/b.flac"]


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="FFmpeg required for this integration test")
def test_real_flac_probe_and_decode(tmp_path):
    path = tmp_path / "tone.flac"
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "flac", str(path),
    ], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    info = quality.probe_flac(path)
    metrics = quality.inspect_signal(path, info["duration_seconds"])
    assert info["codec"] == "flac"
    assert info["duration_seconds"] == 3.0
    assert -22 < metrics["rms_dbfs"] < -20
    assert -19 < metrics["peak_dbfs"] < -17
    assert abs(metrics["dc_offset"]) < 0.01
    assert metrics["silence_seconds"] == 0

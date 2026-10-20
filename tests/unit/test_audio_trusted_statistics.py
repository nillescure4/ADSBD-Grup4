"""Tests for deterministic Trusted audio profiling summaries."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.trusted.audio.audio_quality_report import make_quality_report, read_quality_report, summarize


def fixture_rows():
    """Provide representative non-duplicated and duplicated quality entries."""
    def row(key, status, flags="", duplicate_of=""):
        return {
            "status": status, "source_key": f"audio/{key}.flac", "sha256": key * 64,
            "duplicate_of": duplicate_of, "flags": flags,
            "duration_seconds": "12", "rms_dbfs": "-18", "peak_dbfs": "-1",
            "dc_offset": "0.02" if "dc_offset" in flags else "0.0", "silence_ratio": "0",
        }
    return [row("a", "ok"), row("b", "review", "dc_offset"),
            row("c", "review", "identical_file", "audio/a.flac")]


def test_summary_counts_flags_and_overlapping_reviews():
    summary = summarize(fixture_rows())
    assert summary["total"] == 3
    assert summary["status_counts"]["review"] == 2
    assert summary["duplicate_files"] == 1
    assert summary["unique_file_candidates"] == 2
    assert summary["flag_counts"]["dc_offset"] == 1
    assert summary["metrics"]["duration_seconds"]["median"] == 12


def test_read_rejects_duplicate_keys(tmp_path: Path):
    path = tmp_path / "report.csv"
    rows = fixture_rows()
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows([rows[0], rows[0]])
    with pytest.raises(ValueError, match="duplicated source key"):
        read_quality_report(path)


def test_generate_charts_and_duplicate_map(tmp_path: Path):
    path = tmp_path / "report.csv"
    rows = fixture_rows()
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    output = tmp_path / "analysis"
    summary = make_quality_report(path, output)
    assert summary["duplicate_files"] == 1
    assert json.loads((output / "quality_summary.json").read_text())["total"] == 3
    assert len(list(output.glob("*.png"))) == 5
    assert "audio/a.flac" in (output / "exact_duplicates.csv").read_text()

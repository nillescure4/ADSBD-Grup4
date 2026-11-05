import json

from streamlit.testing.v1 import AppTest

from src.app.views import reports


def media_bundle(modality):
    if modality == "image":
        files = {"profile.csv": b"key,reject_reason\none.jpg,\ntwo.jpg,duplicate\n",
                 "analysis/summary.md": b"# Image quality summary\n\nVerified image assessment."}
    else:
        files = {"profile.csv": b"source_key,status,duplicate_of\none.flac,review,\n",
                 "analysis/quality_overview.md": b"# Audio quality profiling\n\nVerified audio assessment.",
                 "processing.csv": b"status,method\ncopied,copy\n"}
    return {"files": files, "generated_at_utc": "2026-11-05T00:00:00Z"}


def test_reports_render_each_modality_and_keep_missing_sections_independent(monkeypatch):
    def load(modality, storage):
        if modality == "text":
            raise reports.ReportUnavailable("Text analysis is outdated")
        return media_bundle(modality)
    monkeypatch.setattr(reports, "load_report", load)
    monkeypatch.setattr(reports, "report_history", lambda *args: [])
    app = AppTest.from_string("from src.app.views.reports import render\nrender()", default_timeout=15).run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["Descriptions", "Images", "Audio"]
    assert any("outdated" in warning.value for warning in app.warning)
    assert [metric.value for metric in app.metric] == ["2", "1", "1", "1", "1", "0"]
    assert len(app.dataframe) == 2
    assert {widget.key for widget in app.selectbox} >= {"report_revision_text", "report_revision_image", "report_revision_audio"}
    assert any("Verified audio assessment" in item.value for item in app.markdown)


def test_history_selection_loads_the_selected_snapshot(monkeypatch):
    latest, old = "b" * 64, "a" * 64
    history = [{"revision": latest, "generated_at_utc": "2026-11-05T00:00:00Z", "is_latest": True},
               {"revision": old, "generated_at_utc": "2026-11-04T00:00:00Z", "is_latest": False}]
    monkeypatch.setattr(reports, "report_history", lambda modality, storage: history if modality == "image" else [])
    def load(modality, storage, revision=None):
        if modality == "text":
            raise reports.ReportUnavailable("No text report")
        result = media_bundle(modality)
        if modality == "image":
            result.update(revision=revision, current_matches=revision == latest)
            if revision == old:
                result["files"]["profile.csv"] = b"key,reject_reason\none.jpg,\n"
        return result
    monkeypatch.setattr(reports, "load_report", load)
    app = AppTest.from_string("from src.app.views.reports import render\nrender()", default_timeout=15).run()
    assert app.metric[0].value == "2"
    next(widget for widget in app.selectbox if widget.key == "report_revision_image").select_index(1).run()
    assert not app.exception
    assert app.metric[0].value == "1"
    assert any("historical" in item.value.lower() for item in app.info)


def test_text_analysis_uses_english_explanations_and_preserves_source_excerpts():
    report = {"capture_count": 2, "distinct_valid_game_ids": 1, "warnings": 0,
              "completeness": {}, "numeric_profile": {}, "interpretation": ["Antic informe"],
              "qualitative_review": {"samples": []}}
    data, markdown, files = reports.text_analysis({"overview.json": json.dumps(report).encode()})
    assert "Antic informe" not in markdown
    assert "historical dates are included" in markdown
    assert data["capture_count"] == 2
    assert b"Text quality analysis" in files["overview.md"]


def test_download_archive_contains_reports_only():
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(reports.archive({"analysis/summary.md": b"Report"}))) as archive:
        assert archive.namelist() == ["analysis/summary.md"]
        assert archive.read("analysis/summary.md") == b"Report"


def test_selector_recovers_when_first_archived_revision_arrives(monkeypatch):
    history = []
    monkeypatch.setattr(reports, "report_history", lambda modality, storage: list(history) if modality == "image" else [])
    def load(modality, storage, revision=None):
        if modality == "text":
            raise reports.ReportUnavailable("No text report")
        return media_bundle(modality)
    monkeypatch.setattr(reports, "load_report", load)
    app = AppTest.from_string("from src.app.views.reports import render\nrender()", default_timeout=15).run()
    history.append({"revision": "c" * 64, "generated_at_utc": "2026-11-05T00:00:00Z", "is_latest": True})
    app.run()
    assert not app.exception
    assert next(widget for widget in app.selectbox if widget.key == "report_revision_image").value == "c" * 64

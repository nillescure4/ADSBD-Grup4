import json

import pytest

from src.common.quality_reports import ReportUnavailable, digest, load_bundle, load_text_bundle, publish_bundle


STORAGE = {"buckets": {"formatted": "formatted", "trusted": "trusted"}}


def audio_inputs(s3, tmp_path):
    s3.put("formatted", "audio/song.flac", b"audio")
    s3.put("trusted", "audio/song.flac", b"audio")
    etag = s3.head_object(Bucket="formatted", Key="audio/song.flac")["ETag"].strip('"')
    profile = tmp_path / "quality.csv"
    profile.write_text(f"source_key,source_etag\naudio/song.flac,{etag}\n")
    out = tmp_path / "analysis"
    out.mkdir()
    (out / "quality_overview.md").write_text("# Audio quality\n\nMeasured current data.")
    return profile, out


def test_publish_read_and_changed_data_guard(s3, tmp_path):
    profile, out = audio_inputs(s3, tmp_path)
    manifest = publish_bundle(s3, STORAGE, "audio", profile, out)
    bundle = load_bundle(s3, STORAGE, "audio")
    assert bundle["files"]["analysis/quality_overview.md"].startswith(b"# Audio quality")
    assert manifest["generated_at_utc"] == bundle["generated_at_utc"]
    s3.put("trusted", "audio/song.flac", b"changed")
    with pytest.raises(ReportUnavailable, match="outdated"):
        load_bundle(s3, STORAGE, "audio")


def test_corrupt_artifact_is_rejected(s3, tmp_path):
    profile, out = audio_inputs(s3, tmp_path)
    manifest = publish_bundle(s3, STORAGE, "audio", profile, out)
    s3.put("trusted", manifest["files"]["profile.csv"]["key"], b"damaged")
    with pytest.raises(ReportUnavailable, match="integrity"):
        load_bundle(s3, STORAGE, "audio")


def test_wrong_profile_does_not_replace_published_manifest(s3, tmp_path):
    profile, out = audio_inputs(s3, tmp_path)
    publish_bundle(s3, STORAGE, "audio", profile, out)
    key = "quality/reports/audio/manifest.json"
    previous = s3.data("trusted", key)
    profile.write_text("source_key,source_etag\naudio/missing.flac,wrong\n")
    with pytest.raises(ReportUnavailable, match="Formatted"):
        publish_bundle(s3, STORAGE, "audio", profile, out)
    assert s3.data("trusted", key) == previous


def test_text_bundle_checks_partition_coverage_and_content(s3):
    base = "texts/rawg/"
    key = base + "collection_date=2026-11-05/game_details.parquet"
    s3.put("trusted", key, b"parquet")
    checkpoint_key = base + "manifests/date.json"
    checkpoint = json.dumps({"outputs": {key: digest(b"parquet")}}).encode()
    s3.put("trusted", checkpoint_key, checkpoint)
    overview_key = base + "quality/overview.json"
    s3.put("trusted", overview_key, b"{}")
    overview = {"input": {"partitions": [{"date": "2026-11-05", "checkpoint_key": checkpoint_key,
                  "checkpoint_sha256": digest(checkpoint)}]}, "outputs": {overview_key: digest(b"{}")}}
    s3.put("trusted", base + "manifests/quality_overview.json", json.dumps(overview).encode())
    assert load_text_bundle(s3, STORAGE)["files"]["overview.json"] == b"{}"
    s3.put("trusted", key, b"changed")
    with pytest.raises(ReportUnavailable, match="partition"):
        load_text_bundle(s3, STORAGE)


def test_image_publication_requires_the_current_quality_checkpoint(s3, tmp_path):
    from src.trusted.images.build_trusted_images import input_signature, list_objects
    s3.put("formatted", "images/a.jpg", b"image")
    s3.put("trusted", "images/a.jpg", b"image")
    profile = tmp_path / "quality.csv"
    profile.write_text("key,reject_reason\nimages/a.jpg,\n")
    s3.put("trusted", "quality/images/images_quality.csv", profile.read_bytes())
    checkpoint = {"input": input_signature(list_objects(s3, "formatted", "images/"))}
    s3.put("trusted", "quality/images/checkpoint.json", json.dumps(checkpoint).encode())
    out = tmp_path / "analysis"
    out.mkdir()
    (out / "summary.md").write_text("# Images")
    publish_bundle(s3, STORAGE, "image", profile, out)
    assert load_bundle(s3, STORAGE, "image")["files"]["profile.csv"] == profile.read_bytes()
    s3.put("formatted", "images/a.jpg", b"changed")
    with pytest.raises(ReportUnavailable, match="checkpoint"):
        publish_bundle(s3, STORAGE, "image", profile, out)


def test_text_report_history_preserves_previous_revision_and_reuses_identical_content(s3, monkeypatch):
    from src.common import quality_reports as reports
    files = {"overview.json": b'{"quality_version":5}', "overview.md": b"# First analysis"}
    monkeypatch.setattr(reports, "load_text_bundle", lambda *args: {"files": dict(files)})
    first = reports.publish_text_revision(s3, STORAGE)
    assert reports.publish_text_revision(s3, STORAGE)["revision"] == first["revision"]
    files["overview.md"] = b"# Updated analysis with new data"
    second = reports.publish_text_revision(s3, STORAGE)
    assert second["revision"] != first["revision"]
    assert s3.data("trusted", first["files"]["overview.md"]["key"]) == b"# First analysis"
    assert json.loads(s3.data("trusted", "quality/reports/text/manifest.json"))["revision"] == second["revision"]


def test_historical_media_can_be_read_after_new_ingestion(s3, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from src.common import quality_reports as reports
    from src.common.quality_reports import list_revisions
    fixed_time = datetime(2026, 11, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(reports, "datetime", SimpleNamespace(now=lambda tz: fixed_time))
    profile, out = audio_inputs(s3, tmp_path)
    first = publish_bundle(s3, STORAGE, "audio", profile, out)
    s3.put("formatted", "audio/new.flac", b"new")
    s3.put("trusted", "audio/new.flac", b"new")
    old = load_bundle(s3, STORAGE, "audio", first["revision"])
    assert not old["current_matches"]
    assert old["files"]["profile.csv"] == profile.read_bytes()
    etag = s3.head_object(Bucket="formatted", Key="audio/new.flac")["ETag"].strip('"')
    profile.write_text(profile.read_text() + f"audio/new.flac,{etag}\n")
    second = publish_bundle(s3, STORAGE, "audio", profile, out)
    revisions = list_revisions(s3, STORAGE, "audio")
    assert revisions[0]["generated_at_utc"] == revisions[1]["generated_at_utc"]
    assert len(revisions) == 2 and revisions[0]["revision"] == second["revision"]
    assert load_bundle(s3, STORAGE, "audio", second["revision"])["current_matches"]
    assert load_bundle(s3, STORAGE, "audio", first["revision"])["files"]["profile.csv"] != profile.read_bytes()

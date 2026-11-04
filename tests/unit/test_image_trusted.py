"""Unit tests for building the Trusted image zone (metrics are faked; MinIO is in memory)."""

import hashlib
import json
import os
from types import SimpleNamespace

import pandas as pd
import pytest
from botocore.exceptions import ClientError

from src.trusted.images import build_trusted_images as build

FORMATTED = "formatted"
TRUSTED = "trusted"
STORAGE = {"buckets": {"formatted": FORMATTED, "trusted": TRUSTED}}
QUALITY = "quality/images/"


def fake_compute_metrics(raw: bytes) -> dict:
    """Deterministic metrics decided by the content of the fake image.

    b"small..." is too small, b"blank..." has no contrast, b"undecodable" cannot be read, and
    b"twin..." images share a perceptual hash (near duplicates). Any other content is a good image.
    """
    if raw == b"undecodable":
        raise ValueError("cannot decode image")
    seed = b"twin" if raw.startswith(b"twin") else raw
    return {
        "width": 300 if raw.startswith(b"small") else 800, "height": 600, "aspect_ratio": 1.333,
        "file_size": len(raw), "sharpness": 200.0, "brightness": 120.0,
        "contrast": 0.0 if raw.startswith(b"blank") else 40.0, "entropy": 6.0, "letterbox_ratio": 0.0,
        "sha256": hashlib.sha256(raw).hexdigest(), "phash": hashlib.sha256(seed).hexdigest()[:16],
    }


@pytest.fixture
def pipeline(monkeypatch, s3, tmp_path):
    monkeypatch.setattr(build, "load_storage_config", lambda: STORAGE)
    monkeypatch.setattr(build, "create_minio_client", lambda: s3)
    measured = []

    def counting_metrics(raw):
        measured.append(raw)
        return fake_compute_metrics(raw)

    monkeypatch.setattr(build, "compute_metrics", counting_metrics)
    csv = tmp_path / "reports" / "images_quality.csv"

    def run(**options):
        options.setdefault("dry_run", False)
        options.setdefault("prune", False)
        return build.build_trusted_images(csv_output=csv, **options)

    return SimpleNamespace(s3=s3, measured=measured, csv=csv, run=run)


def put_formatted(s3, **images):
    """put_formatted(s3, a=b"content") stores images/a.jpg in the Formatted bucket."""
    for name, content in images.items():
        s3.put(FORMATTED, f"images/{name}.jpg", content)


def trusted_images(s3):
    return s3.keys(TRUSTED, "images/")


def read_json(s3, key):
    return json.loads(s3.data(TRUSTED, key))


# ---- helpers ---------------------------------------------------------------------------------


def test_list_objects_returns_key_to_etag_for_every_page(s3):
    for index in range(5):
        s3.put(FORMATTED, f"images/{index}.jpg", bytes([index]))
    s3.put(FORMATTED, "other/9.jpg", b"elsewhere")
    listing = build.list_objects(s3, FORMATTED, "images/")
    assert sorted(listing) == [f"images/{index}.jpg" for index in range(5)]
    assert all(etag.startswith('"') for etag in listing.values())


def test_read_json_or_none(s3):
    s3.put(TRUSTED, "a.json", b'{"x": 1}')
    assert build.read_json_or_none(s3, TRUSTED, "a.json") == {"x": 1}
    assert build.read_json_or_none(s3, TRUSTED, "missing.json") is None


def test_read_json_or_none_does_not_hide_other_errors(s3):
    s3.put(TRUSTED, "a.json", b"{}")
    s3.failures[("get_object", "a.json")] = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    with pytest.raises(ClientError):
        build.read_json_or_none(s3, TRUSTED, "a.json")


def test_put_verified_detects_a_truncated_upload(s3, monkeypatch):
    original = type(s3).put_object
    monkeypatch.setattr(
        s3, "put_object",
        lambda Bucket, Key, Body, ContentType=None: original(s3, Bucket, Key, Body[:-1], ContentType),
    )
    with pytest.raises(ValueError, match="could not verify"):
        build.put_verified(s3, TRUSTED, "report.json", b"complete content", "application/json")


# ---- input signature ----------------------------------------------------------------------------------


LISTING = {"images/a.jpg": '"1"', "images/b.jpg": '"2"'}


def test_signature_ignores_listing_order():
    assert build.input_signature(LISTING) == build.input_signature(dict(reversed(list(LISTING.items()))))


@pytest.mark.parametrize(
    "changed",
    [
        {**LISTING, "images/b.jpg": '"changed"'},   # an image was replaced
        {**LISTING, "images/c.jpg": '"3"'},         # an image was added
        {"images/a.jpg": '"1"'},                    # an image was removed
    ],
)
def test_signature_changes_with_the_input_images(changed):
    assert build.input_signature(changed) != build.input_signature(LISTING)


def test_signature_changes_with_the_rules_version(monkeypatch):
    before = build.input_signature(LISTING)
    monkeypatch.setattr(build.rules, "RULES_VERSION", build.rules.RULES_VERSION + 1)
    assert build.input_signature(LISTING) != before


def test_signature_changes_with_a_threshold(monkeypatch):
    before = build.input_signature(LISTING)
    monkeypatch.setitem(build.rules.THRESHOLDS, "min_side", 100)
    assert build.input_signature(LISTING) != before


# ---- sync_trusted ---------------------------------------------------------------------------------------


def sync(s3, accepted, **options):
    formatted = build.list_objects(s3, FORMATTED, "images/")
    options.setdefault("dry_run", False)
    options.setdefault("prune", False)
    return build.sync_trusted(s3, FORMATTED, TRUSTED, formatted, accepted, **options)


def test_sync_copies_only_the_accepted_images(s3):
    put_formatted(s3, a=b"A", b=b"B", c=b"C")
    assert sync(s3, ["images/a.jpg", "images/c.jpg"]) == 0
    assert trusted_images(s3) == ["images/a.jpg", "images/c.jpg"]


def test_sync_skips_images_that_are_already_up_to_date(s3):
    put_formatted(s3, a=b"A")
    s3.put(TRUSTED, "images/a.jpg", b"A")
    assert sync(s3, ["images/a.jpg"]) == 0
    assert s3.calls["copy_object"] == 0


def test_sync_replaces_an_image_that_changed_in_formatted(s3):
    put_formatted(s3, a=b"new")
    s3.put(TRUSTED, "images/a.jpg", b"old")
    assert sync(s3, ["images/a.jpg"]) == 0
    assert s3.data(TRUSTED, "images/a.jpg") == b"new"


def test_sync_keeps_stale_images_unless_pruning(s3):
    put_formatted(s3, a=b"A")
    s3.put(TRUSTED, "images/stale.jpg", b"S")
    assert sync(s3, ["images/a.jpg"]) == 0
    assert trusted_images(s3) == ["images/a.jpg", "images/stale.jpg"]
    assert sync(s3, ["images/a.jpg"], prune=True) == 0
    assert trusted_images(s3) == ["images/a.jpg"]


def test_sync_dry_run_neither_copies_nor_deletes(s3):
    put_formatted(s3, a=b"A")
    s3.put(TRUSTED, "images/stale.jpg", b"S")
    assert sync(s3, ["images/a.jpg"], dry_run=True, prune=True) == 0
    assert trusted_images(s3) == ["images/stale.jpg"]


def test_sync_reports_a_damaged_copy_as_an_error(s3):
    put_formatted(s3, a=b"A")
    s3.corrupt_copies = True
    assert sync(s3, ["images/a.jpg"]) == 1


def test_sync_counts_a_failed_delete_as_an_error(s3):
    s3.put(TRUSTED, "images/stale.jpg", b"S")
    s3.failures[("delete_object", "images/stale.jpg")] = RuntimeError("boom")
    assert sync(s3, [], prune=True) == 1


# ---- the whole step --------------------------------------------------------------------------------------


def test_only_accepted_images_reach_trusted(pipeline):
    put_formatted(pipeline.s3, good1=b"one", good2=b"two", small=b"small image",
                  blank=b"blank image", broken=b"undecodable")
    assert pipeline.run() == 0
    assert trusted_images(pipeline.s3) == ["images/good1.jpg", "images/good2.jpg"]
    assert pipeline.s3.data(TRUSTED, "images/good1.jpg") == b"one"


def test_unreadable_images_are_rejected_not_pipeline_failures(pipeline):
    put_formatted(pipeline.s3, broken=b"undecodable")
    assert pipeline.run() == 0
    summary = read_json(pipeline.s3, f"{QUALITY}summary.json")
    assert summary["outcomes"] == {"corrupt": 1}


def test_an_unreadable_image_aborts_the_run_without_writing_anything(pipeline):
    """A MinIO read error is not a corrupt image: nothing may be decided or checkpointed."""
    put_formatted(pipeline.s3, a=b"one", b=b"two")
    pipeline.s3.failures[("get_object", "images/b.jpg")] = ConnectionError("network down")
    assert pipeline.run() == 1
    assert pipeline.s3.keys(TRUSTED) == []
    assert not pipeline.csv.exists()


def test_an_image_that_failed_to_read_is_picked_up_by_the_next_run(pipeline):
    put_formatted(pipeline.s3, a=b"one", b=b"two")
    pipeline.s3.failures[("get_object", "images/b.jpg")] = ConnectionError("network down")
    pipeline.run()
    del pipeline.s3.failures[("get_object", "images/b.jpg")]
    assert pipeline.run() == 0
    assert trusted_images(pipeline.s3) == ["images/a.jpg", "images/b.jpg"]


def test_duplicates_keep_the_first_image_in_key_order(pipeline):
    put_formatted(pipeline.s3, a=b"same", b=b"same", c=b"twin 1", d=b"twin 2")
    pipeline.run()
    assert trusted_images(pipeline.s3) == ["images/a.jpg", "images/c.jpg"]


def test_reports_are_written_locally_and_to_the_trusted_bucket(pipeline):
    put_formatted(pipeline.s3, good=b"one", small=b"small image", blank=b"blank image", broken=b"undecodable")
    pipeline.run()
    local = pd.read_csv(pipeline.csv)
    assert sorted(local["key"]) == ["images/blank.jpg", "images/broken.jpg", "images/good.jpg", "images/small.jpg"]
    assert pipeline.s3.data(TRUSTED, f"{QUALITY}images_quality.csv") == pipeline.csv.read_bytes()
    summary = read_json(pipeline.s3, f"{QUALITY}summary.json")
    assert summary["images_seen"] == 4 and summary["accepted"] == 1
    assert summary["outcomes"] == {"accepted": 1, "too_small": 1, "blank": 1, "corrupt": 1}
    assert summary["thresholds"] == build.rules.THRESHOLDS
    assert summary["rules_version"] == build.rules.RULES_VERSION


def test_csv_has_one_line_ending_per_row_on_every_platform(pipeline, monkeypatch):
    """Regression: on Windows the local CSV got '\\r\\r\\n', a blank line after every row."""
    monkeypatch.setattr(os, "linesep", "\r\n")  # what pandas uses to end lines on Windows
    put_formatted(pipeline.s3, a=b"one", b=b"two")
    pipeline.run()
    local = pipeline.csv.read_bytes()
    assert b"\r" not in local
    assert local.count(b"\n") == 3  # header + two images, no blank lines
    assert pipeline.s3.data(TRUSTED, f"{QUALITY}images_quality.csv") == local


def test_checkpoint_records_the_decision(pipeline):
    put_formatted(pipeline.s3, a=b"one", b=b"small image")
    pipeline.run()
    checkpoint = read_json(pipeline.s3, f"{QUALITY}checkpoint.json")
    assert checkpoint["accepted"] == ["images/a.jpg"]
    assert checkpoint["input"] == build.input_signature(build.list_objects(pipeline.s3, FORMATTED, "images/"))


def test_second_run_measures_nothing(pipeline):
    put_formatted(pipeline.s3, a=b"one", b=b"two")
    pipeline.run()
    assert len(pipeline.measured) == 2
    assert pipeline.run() == 0
    assert len(pipeline.measured) == 2


def test_unchanged_run_restores_deleted_local_quality_report(pipeline):
    put_formatted(pipeline.s3, a=b"one")
    assert pipeline.run() == 0
    original = pipeline.csv.read_bytes()
    pipeline.csv.unlink()
    assert pipeline.run() == 0
    assert pipeline.csv.read_bytes() == original
    assert len(pipeline.measured) == 1


def test_second_run_repairs_a_missing_trusted_image_without_measuring(pipeline):
    put_formatted(pipeline.s3, a=b"one", b=b"two")
    pipeline.run()
    pipeline.s3.delete_object(Bucket=TRUSTED, Key="images/b.jpg")
    assert pipeline.run() == 0
    assert trusted_images(pipeline.s3) == ["images/a.jpg", "images/b.jpg"]
    assert len(pipeline.measured) == 2


def test_a_new_image_triggers_a_full_evaluation(pipeline):
    put_formatted(pipeline.s3, a=b"one")
    pipeline.run()
    put_formatted(pipeline.s3, b=b"two")
    pipeline.run()
    assert len(pipeline.measured) == 3  # a, then a and b again
    assert trusted_images(pipeline.s3) == ["images/a.jpg", "images/b.jpg"]


def test_a_new_rules_version_triggers_a_full_evaluation(pipeline, monkeypatch):
    put_formatted(pipeline.s3, a=b"one")
    pipeline.run()
    monkeypatch.setattr(build.rules, "RULES_VERSION", build.rules.RULES_VERSION + 1)
    pipeline.run()
    assert len(pipeline.measured) == 2


def test_a_replaced_formatted_image_is_measured_again_and_overwritten(pipeline):
    put_formatted(pipeline.s3, a=b"one")
    pipeline.run()
    put_formatted(pipeline.s3, a=b"one but different")
    pipeline.run()
    assert len(pipeline.measured) == 2
    assert pipeline.s3.data(TRUSTED, "images/a.jpg") == b"one but different"


def test_dry_run_writes_nothing(pipeline):
    put_formatted(pipeline.s3, a=b"one", b=b"small image")
    assert pipeline.run(dry_run=True) == 0
    assert pipeline.s3.keys(TRUSTED) == []
    assert not pipeline.csv.exists()
    assert len(pipeline.measured) == 2


def test_a_failed_copy_fails_the_run_and_the_next_run_retries(pipeline):
    put_formatted(pipeline.s3, a=b"one", b=b"two")
    pipeline.s3.failures[("copy_object", "images/b.jpg")] = RuntimeError("boom")
    assert pipeline.run() == 1
    assert trusted_images(pipeline.s3) == ["images/a.jpg"]
    assert pipeline.s3.keys(TRUSTED, f"{QUALITY}checkpoint") == []
    del pipeline.s3.failures[("copy_object", "images/b.jpg")]
    assert pipeline.run() == 0
    assert trusted_images(pipeline.s3) == ["images/a.jpg", "images/b.jpg"]
    assert len(pipeline.s3.keys(TRUSTED, f"{QUALITY}checkpoint")) == 1


def test_an_image_that_stops_being_accepted_stays_until_pruned(pipeline):
    """A new image sorting before its exact duplicate takes over, leaving the old copy stale."""
    put_formatted(pipeline.s3, b=b"same")
    pipeline.run()
    put_formatted(pipeline.s3, a=b"same")
    assert pipeline.run() == 0
    assert trusted_images(pipeline.s3) == ["images/a.jpg", "images/b.jpg"]
    assert pipeline.run(prune=True) == 0
    assert trusted_images(pipeline.s3) == ["images/a.jpg"]


def test_empty_formatted_zone_is_not_an_error(pipeline):
    assert pipeline.run() == 0
    assert pipeline.s3.keys(TRUSTED) == []


def test_reports_do_not_live_under_the_images_prefix(pipeline):
    put_formatted(pipeline.s3, a=b"one")
    pipeline.run()
    assert all(not key.startswith("images/") for key in pipeline.s3.keys(TRUSTED, QUALITY))
    assert trusted_images(pipeline.s3) == ["images/a.jpg"]


def test_the_bucket_is_checked_before_doing_anything(pipeline):
    put_formatted(pipeline.s3, a=b"one")
    pipeline.run()
    assert pipeline.s3.calls["head_bucket"] == 1


def test_end_to_end_with_real_images(monkeypatch, s3, tmp_path, image_bytes):
    """One run with the real metrics: a good image, a duplicate, and an image that is too small."""
    monkeypatch.setattr(build, "load_storage_config", lambda: STORAGE)
    monkeypatch.setattr(build, "create_minio_client", lambda: s3)
    s3.put(FORMATTED, "images/a_good.jpg", image_bytes(seed=1, size=(640, 640), fmt="JPEG"))
    s3.put(FORMATTED, "images/b_copy.jpg", image_bytes(seed=1, size=(640, 640), fmt="JPEG"))
    s3.put(FORMATTED, "images/c_small.jpg", image_bytes(seed=2, size=(200, 200), fmt="JPEG"))
    assert build.build_trusted_images(dry_run=False, prune=False, csv_output=tmp_path / "q.csv") == 0
    assert trusted_images(s3) == ["images/a_good.jpg"]
    outcomes = read_json(s3, f"{QUALITY}summary.json")["outcomes"]
    assert outcomes == {"accepted": 1, "duplicate": 1, "too_small": 1}

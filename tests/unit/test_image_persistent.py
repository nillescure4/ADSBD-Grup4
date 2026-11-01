"""Unit tests for moving images from Temporal to Persistent Landing."""

from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

# Adjust this import if your module has another name.
from src.ingestion.images import images_temporal_to_persistent as persistent

BUCKET = "landing"
DATASET = {"modality": "image", "directory": "games", "source": "rawg"}
ACCESS_DENIED = ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
MODIFIED = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def move_games(s3, *, dry_run=False):
    return persistent.move_dataset_to_persistent(
        s3, BUCKET, "temporal", "persistent", "games", DATASET, dry_run
    )


# ---- standard name -----------------------------------------------------------------


def test_standard_name_has_source_dataset_name_and_date():
    name = persistent.build_standard_name("rawg", "games", "temporal/images/games/123.PNG", MODIFIED)
    assert name == "rawg$games$123$18-10-2026.png"


def test_standard_name_keeps_dots_inside_the_stem():
    name = persistent.build_standard_name("rawg", "games", "temporal/images/games/a.b.jpg", MODIFIED)
    assert name == "rawg$games$a.b$18-10-2026.jpg"


def test_standard_name_uses_the_utc_date_not_the_local_one():
    late_evening_west = datetime(2026, 10, 18, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    name = persistent.build_standard_name("rawg", "games", "temporal/images/games/1.jpg", late_evening_west)
    assert name.endswith("$19-10-2026.jpg")


# ---- metadata helpers -----------------------------------------------------------------


def test_head_or_none_returns_none_only_for_missing_keys(s3):
    s3.put(BUCKET, "present.jpg", b"abc")
    assert persistent.head_or_none(s3, BUCKET, "present.jpg")["ContentLength"] == 3
    assert persistent.head_or_none(s3, BUCKET, "absent.jpg") is None


def test_head_or_none_does_not_hide_other_errors(s3):
    s3.put(BUCKET, "present.jpg", b"abc")
    s3.failures[("head_object", "present.jpg")] = ACCESS_DENIED
    with pytest.raises(ClientError):
        persistent.head_or_none(s3, BUCKET, "present.jpg")


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        ({"ContentLength": 3, "ETag": '"a"'}, True),
        ({"ContentLength": 4, "ETag": '"a"'}, False),
        ({"ContentLength": 3, "ETag": '"b"'}, False),
    ],
)
def test_same_object_compares_size_and_etag(other, expected):
    assert persistent.same_object({"ContentLength": 3, "ETag": '"a"'}, other) is expected


# ---- moving one object -------------------------------------------------------------------


def test_move_object_copies_then_deletes_the_source(s3):
    s3.put(BUCKET, "temporal/a.jpg", b"pixels")
    persistent.move_object(s3, BUCKET, "temporal/a.jpg", "persistent/a.jpg")
    assert s3.keys(BUCKET) == ["persistent/a.jpg"]
    assert s3.data(BUCKET, "persistent/a.jpg") == b"pixels"


def test_move_object_resumes_after_an_interrupted_run(s3):
    """The copy already exists but the source was never deleted."""
    s3.put(BUCKET, "temporal/a.jpg", b"pixels")
    s3.put(BUCKET, "persistent/a.jpg", b"pixels")
    persistent.move_object(s3, BUCKET, "temporal/a.jpg", "persistent/a.jpg")
    assert s3.calls["copy_object"] == 0
    assert s3.keys(BUCKET) == ["persistent/a.jpg"]


def test_move_object_never_overwrites_different_content(s3):
    s3.put(BUCKET, "temporal/a.jpg", b"new pixels")
    s3.put(BUCKET, "persistent/a.jpg", b"old pixels")
    with pytest.raises(ValueError, match="different content"):
        persistent.move_object(s3, BUCKET, "temporal/a.jpg", "persistent/a.jpg")
    assert s3.data(BUCKET, "persistent/a.jpg") == b"old pixels"
    assert s3.keys(BUCKET) == ["persistent/a.jpg", "temporal/a.jpg"]


def test_move_object_keeps_the_source_when_the_copy_is_damaged(s3):
    s3.put(BUCKET, "temporal/a.jpg", b"pixels")
    s3.corrupt_copies = True
    with pytest.raises(ValueError, match="does not match"):
        persistent.move_object(s3, BUCKET, "temporal/a.jpg", "persistent/a.jpg")
    assert s3.data(BUCKET, "temporal/a.jpg") == b"pixels"


# ---- moving a dataset -------------------------------------------------------------------------


def test_dataset_images_are_renamed_and_moved(s3):
    s3.put(BUCKET, "temporal/images/games/1.jpg", b"one")
    s3.put(BUCKET, "temporal/images/games/2.png", b"two")
    assert move_games(s3) == (2, 0)
    assert s3.keys(BUCKET) == [
        "persistent/images/rawg$games$1$18-10-2026.jpg",
        "persistent/images/rawg$games$2$18-10-2026.png",
    ]


def test_other_datasets_and_non_images_are_left_alone(s3):
    s3.put(BUCKET, "temporal/images/games/1.jpg", b"one")
    s3.put(BUCKET, "temporal/images/games/notes.txt", b"text")
    s3.put(BUCKET, "temporal/images/games2/5.jpg", b"other dataset with a similar prefix")
    s3.put(BUCKET, "temporal/audio/games/7.wav", b"other modality")
    assert move_games(s3) == (1, 0)
    assert s3.keys(BUCKET, "temporal/") == [
        "temporal/audio/games/7.wav",
        "temporal/images/games/notes.txt",
        "temporal/images/games2/5.jpg",
    ]


def test_every_page_of_the_listing_is_processed(s3):
    for index in range(5):
        s3.put(BUCKET, f"temporal/images/games/{index}.jpg", b"x" * (index + 1))
    assert move_games(s3) == (5, 0)
    assert len(s3.keys(BUCKET, "persistent/images/")) == 5
    assert s3.keys(BUCKET, "temporal/") == []


def test_dry_run_changes_nothing(s3):
    s3.put(BUCKET, "temporal/images/games/1.jpg", b"one")
    assert move_games(s3, dry_run=True) == (0, 0)
    assert s3.keys(BUCKET) == ["temporal/images/games/1.jpg"]


def test_one_failure_does_not_stop_the_rest(s3):
    s3.put(BUCKET, "temporal/images/games/1.jpg", b"one")
    s3.put(BUCKET, "temporal/images/games/2.jpg", b"two")
    s3.put(BUCKET, "temporal/images/games/3.jpg", b"three")
    s3.failures[("copy_object", "persistent/images/rawg$games$2$18-10-2026.jpg")] = RuntimeError("boom")
    assert move_games(s3) == (2, 1)
    assert "temporal/images/games/2.jpg" in s3.keys(BUCKET)  # the failed one stays in temporal
    assert "temporal/images/games/1.jpg" not in s3.keys(BUCKET)


def test_rerun_after_a_failure_finishes_the_job(s3):
    s3.put(BUCKET, "temporal/images/games/1.jpg", b"one")
    s3.failures[("delete_object", "temporal/images/games/1.jpg")] = RuntimeError("boom")
    assert move_games(s3) == (0, 1)  # copied, but the source could not be deleted
    del s3.failures[("delete_object", "temporal/images/games/1.jpg")]
    assert move_games(s3) == (1, 0)
    assert s3.keys(BUCKET) == ["persistent/images/rawg$games$1$18-10-2026.jpg"]


def test_empty_dataset_moves_nothing(s3):
    assert move_games(s3) == (0, 0)
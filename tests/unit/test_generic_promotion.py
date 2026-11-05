"""The compatibility CLI has the same safe promotion contract as modality scripts."""

import pytest

from src.ingestion import temporal_to_persistent as promotion


@pytest.fixture
def setup(monkeypatch, s3):
    monkeypatch.setattr(promotion, "create_minio_client", lambda: s3)
    monkeypatch.setattr(promotion, "load_storage_config", lambda: {
        "buckets": {"landing": "landing"},
        "landing": {"temporal_prefix": "temporal", "persistent_prefix": "persistent"},
    })
    return s3


def test_conflicting_destination_keeps_both_originals(setup):
    setup.put("landing", "temporal/source", b"new")
    setup.put("landing", "persistent/destination", b"old")
    with pytest.raises(ValueError, match="different content"):
        promotion.promote_object("temporal/source", "persistent/destination")
    assert setup.data("landing", "temporal/source") == b"new"
    assert setup.data("landing", "persistent/destination") == b"old"


def test_damaged_copy_does_not_remove_temporal(setup):
    setup.put("landing", "temporal/source", b"data")
    setup.corrupt_copies = True
    with pytest.raises(ValueError, match="copy does not match"):
        promotion.promote_object("temporal/source", "persistent/destination")
    assert setup.data("landing", "temporal/source") == b"data"


def test_identical_retry_verifies_and_removes_only_temporal(setup):
    setup.put("landing", "temporal/source", b"data")
    setup.put("landing", "persistent/destination", b"data")
    promotion.promote_object("temporal/source", "persistent/destination")
    assert setup.keys("landing") == ["persistent/destination"]

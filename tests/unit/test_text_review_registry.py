import hashlib
import json

import pytest

from src.common.config import PROJECT_ROOT
from src.trusted import text_review_registry as registry
from src.trusted.text_quality import clean_description


def test_documented_pairs_match_real_backup_and_complete_clean_text():
    sources = {}
    for path in (PROJECT_ROOT / "backup_data/texts/raw_api").rglob("*.json"):
        body = path.read_bytes()
        sources[hashlib.sha256(body).hexdigest()] = json.loads(body)
    records = registry.reviews()
    assert len(records) == 8
    for record in records:
        payload = sources[record["source_sha256"]]
        text = clean_description(payload[record["selected_field"]])
        assert registry.documented_review(payload["id"], record["source_sha256"], text) == record
        assert registry.documented_review(payload["id"], "changed", text) is None
        assert registry.documented_review(payload["id"], record["source_sha256"], text + " changed") is None


@pytest.mark.parametrize(("statuses", "status", "count"), [
    ([], "pending_human_review", 0),
    (["pending_human_review"], "pending_human_review", 0),
    (["documented_assisted_review", "pending_human_review"], "partial_sample_review", 1),
    (["documented_assisted_review"], "documented_sample_review", 1),
])
def test_review_summary_does_not_approve_unreviewed_samples(statuses, status, count):
    result = registry.review_summary([{"review_status": value} for value in statuses])
    assert result["status"] == status
    assert result["documented_samples"] == count
    assert result["pending_samples"] == len(statuses) - count


def test_registry_edit_invalidates_digest_and_is_seen_without_process_restart(tmp_path, monkeypatch):
    path = tmp_path / "reviews.json"
    monkeypatch.setattr(registry, "REGISTRY", path)
    path.write_text('{"reviews": []}', encoding="utf-8")
    previous = registry.registry_digest()
    assert registry.reviews() == []
    path.write_text('{"reviews": [{"game_id": 1}]}', encoding="utf-8")
    assert registry.registry_digest() != previous
    assert registry.reviews() == [{"game_id": 1}]

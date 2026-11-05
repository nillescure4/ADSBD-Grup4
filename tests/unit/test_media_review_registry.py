import hashlib
import json

from src.common.config import PROJECT_ROOT
from src.trusted.media_review_registry import REGISTRY, matching_reviews, review_markdown


def test_documented_reviews_bind_to_real_sources_and_exact_formatted_hashes():
    report = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for modality, count in (("image", 8), ("audio", 4)):
        rows = report["assisted_reviews"][modality]
        assert len(rows) == count
        for row in rows:
            source = PROJECT_ROOT / "backup_data" / row["source_path"]
            assert hashlib.sha256(source.read_bytes()).hexdigest() == row["source_sha256"]
            assert row in matching_reviews(modality, {row["formatted_sha256"]})
        assert matching_reviews(modality, {"changed-content-hash"}) == []
        assert "Other content remains unreviewed" in "\n".join(review_markdown(modality, []))


def test_audio_evidence_distinguishes_gain_from_pcm_quantization():
    report = json.loads(REGISTRY.read_text(encoding="utf-8"))
    rows = report["audio"]
    assert len(rows) == 16
    assert sum(row["conversion_gain_db"] < 0 for row in rows) == 2
    assert all(row["gain_aligned_pcm_difference"]["max_absolute_error"] <= 1 / 65536 for row in rows)
    assert all(row["trusted_pcm_difference"]["identical"] for row in rows)
    assert all(row["source_sample_rate_hz"] == row["sample_rate_hz"]
               and row["source_channels"] == row["channels"] for row in rows)


def test_changed_processing_does_not_reuse_old_quality_decisions(tmp_path, monkeypatch):
    from src.trusted import media_review_registry as registry
    report = json.loads(REGISTRY.read_text(encoding="utf-8"))
    original_hash = report["assisted_reviews"]["image"][0]["formatted_sha256"]
    report["processing"]["image_rules_version"] = -1
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(registry, "REGISTRY", path)
    assert registry.matching_reviews("image", {original_hash}) == []

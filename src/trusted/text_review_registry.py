"""Attach documented sample reviews only to the exact source and cleaned bytes."""

import hashlib
import json

from src.common.config import PROJECT_ROOT


REGISTRY = PROJECT_ROOT / "docs/quality/text_sample_reviews.json"


def registry_digest():
    return hashlib.sha256(REGISTRY.read_bytes()).hexdigest() if REGISTRY.is_file() else "unavailable"


def reviews():
    if not REGISTRY.is_file():
        return []
    return json.loads(REGISTRY.read_text(encoding="utf-8"))["reviews"]


def documented_review(game_id, source_hash, clean_text):
    digest = hashlib.sha256((clean_text or "").encode("utf-8")).hexdigest()
    return next((row for row in reviews() if row["game_id"] == game_id
                 and row["source_sha256"] == source_hash and row["clean_sha256"] == digest), None)


def review_summary(samples):
    reviewed = sum(sample["review_status"] == "documented_assisted_review" for sample in samples)
    return {"status": "documented_sample_review" if samples and reviewed == len(samples)
            else "partial_sample_review" if reviewed else "pending_human_review",
            "scope": "selected samples only; no semantic certification of the whole dataset",
            "documented_samples": reviewed, "pending_samples": len(samples) - reviewed, "samples": samples}

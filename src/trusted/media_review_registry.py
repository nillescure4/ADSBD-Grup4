"""Expose sample-scoped assessments only for exact verified media hashes."""

import json

from src.common.config import PROJECT_ROOT


REGISTRY = PROJECT_ROOT / "docs/quality/media_sample_reviews.json"


def current_processing():
    from src.trusted.images.rules import RULES_VERSION, THRESHOLDS
    from src.formatting.audio.formatting_audio import FORMAT_VERSION
    from src.trusted.audio.trusted_audio import POLICY_VERSION
    return {"image_rules_version": RULES_VERSION, "image_thresholds": THRESHOLDS,
            "audio_format_version": FORMAT_VERSION, "audio_trusted_policy": POLICY_VERSION}


def matching_reviews(modality, hashes):
    if not REGISTRY.is_file():
        return []
    document = json.loads(REGISTRY.read_text(encoding="utf-8"))
    expected = current_processing()
    fields = ("image_rules_version", "image_thresholds") if modality == "image" else ("audio_format_version", "audio_trusted_policy")
    if any(document.get("processing", {}).get(field) != expected[field] for field in fields):
        return []
    records = document["assisted_reviews"][modality]
    return [row for row in records if row["formatted_sha256"] in hashes]


def review_markdown(modality, hashes):
    matched = matching_reviews(modality, set(hashes))
    lines = ["", "## Documented qualitative sample assessments", "",
             f"Exact matching reviewed samples: {len(matched)}. Other content remains unreviewed.",
             "These are assisted assessments of selected media, not human certification of the corpus. "
             "Comparisons reconstruct production conversion/cleaning from the documented originals; "
             "they do not assert that a current MinIO object was inspected."]
    for row in matched:
        assessment = row["assessment"]
        lines += ["", f"### {row['source_path']}", "",
                  f"Formatted SHA-256: `{row['formatted_sha256']}`. Reviewer: {row['reviewer']}."]
        for field in ("observed_content", "before_after", "quality_decision", "task_effect", "limitations"):
            lines.append(f"- {field.replace('_', ' ').capitalize()}: {assessment[field]}")
    return lines

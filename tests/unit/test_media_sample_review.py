import numpy as np
import pytest

from src.trusted.review_support import difference, select_samples


def test_comparison_requires_identical_shape_and_measures_all_samples():
    before = np.array([0., 1., -1.], dtype=np.float32)
    assert difference(before, before)["identical"]
    changed = difference(before, np.array([0., 1., 0.], dtype=np.float32))
    assert not changed["identical"]
    assert changed["max_absolute_error"] == 1
    assert changed["rms_error"] == pytest.approx(1 / np.sqrt(3))
    with pytest.raises(ValueError, match="different shapes"):
        difference(before, before[:2])


def test_sampling_covers_extremes_without_duplicate_approvals():
    rows = [{"sample_id": str(i), "brightness": i, "sharpness": 10 - i} for i in range(6)]
    selected = select_samples(rows, ["brightness", "sharpness"], 4)
    assert len(selected) == len({r["sample_id"] for r in selected}) == 4
    assert {"0", "5"} <= {r["sample_id"] for r in selected}
    assert select_samples([], ["brightness"], 4) == []


def test_orchestrator_preserves_measurement_contract_without_calling_ai(tmp_path, monkeypatch):
    import json
    import sys
    from src.trusted import media_sample_review as review
    calls = []
    def collect(modality):
        def run(root, out):
            calls.append(modality)
            return [{"sample_id": modality}], [({"sample_id": modality}, [])]
        return run
    monkeypatch.setattr(review, "image_evidence", collect("image"))
    monkeypatch.setattr(review, "audio_evidence", collect("audio"))
    monkeypatch.setattr(review, "validate_backup", lambda root: None)
    monkeypatch.setattr(review, "assess_images", lambda assets: pytest.fail("AI must be opt-in"))
    monkeypatch.setattr(review, "assess_audio", lambda assets: pytest.fail("AI must be opt-in"))
    monkeypatch.setattr(review.subprocess, "check_output", lambda *args, **kwargs: "test-ffmpeg\n")
    monkeypatch.setattr(sys, "argv", ["media_sample_review", "--output-dir", str(tmp_path)])
    review.main()
    report = json.loads((tmp_path / "measurements.json").read_text())
    assert calls == ["image", "audio"]
    assert report["images"] == [{"sample_id": "image"}]
    assert report["audio"] == [{"sample_id": "audio"}]
    assert report["selected_image_ids"] == ["image"]
    assert report["selected_audio_ids"] == ["audio"]
    assert not (tmp_path / "assessed_samples.json").exists()


@pytest.mark.parametrize(("modality", "mime"), [("image", "image/jpeg"), ("audio", "audio/wav")])
def test_format_specific_assessment_keeps_attachment_types(tmp_path, monkeypatch, modality, mime):
    import base64
    from types import SimpleNamespace
    from src.app import generative_model
    from src.trusted.images.sample_review import assess_images
    from src.trusted.audio.sample_review import assess_audio
    media = tmp_path / ("pair.jpg" if modality == "image" else "excerpt.wav")
    media.write_bytes(b"original-media")
    monkeypatch.setattr(generative_model, "configuration", lambda: (SimpleNamespace(provider="gemini", model="test-model"), None))
    def generate(config, system, prompt, schema, *, images):
        assert all(attachment["mime_type"] == mime for attachment in images)
        assert base64.b64decode(images[0]["data"]) == b"original-media"
        return {"assessments": [{"sample_id": "1", "observed_content": "Sample observation"}]}
    monkeypatch.setattr(generative_model, "generate", generate)
    row = {"sample_id": "1", "source_sha256": "source"}
    result = assess_images([(row, media)]) if modality == "image" else assess_audio([(row, [media, media])])
    assert result[0]["source_sha256"] == "source"
    assert result[0]["reviewer"] == "Gemini-assisted: test-model"

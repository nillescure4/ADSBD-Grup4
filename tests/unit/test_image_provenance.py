"""Reject incorrect lineage before attaching a game to a screenshot."""

import hashlib
import io
import json

from PIL import Image
import pytest

from src.trusted.images import publish_provenance as provenance


@pytest.fixture
def inputs(tmp_path, s3):
    root = tmp_path / "rawg"
    (root / "manifests").mkdir(parents=True)
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(buffer, format="PNG")
    raw = buffer.getvalue()
    row = {"source_game_id": 28, "name": "A game", "local_path": "images/28/shot_01_hash.png",
           "sha256": hashlib.sha256(raw).hexdigest(), "source_url": "https://example.com/image.png"}
    (root / "manifests/images.jsonl").write_text(json.dumps(row) + "\n")
    storage = {"buckets": {"landing": "landing", "formatted": "formatted", "trusted": "trusted"},
               "landing": {"persistent_prefix": "persistent"}}
    original = "persistent/images/rawg$rawg_images$shot_01_hash$04-11-2026.png"
    target = "images/rawg$rawg_images$shot_01_hash$04-11-2026.jpg"
    s3.put("landing", original, raw)
    jpeg = provenance.convert_to_jpeg(raw)
    s3.put("formatted", target, jpeg)
    s3.put("trusted", target, jpeg)
    return root, storage, original, target


def test_verified_association_and_unchanged_run_does_not_write(inputs, s3):
    root, storage, _, target = inputs
    assert provenance.publish(s3, storage, root, "rawg_images") == 1
    row = json.loads(s3.data("trusted", "provenance/images.jsonl"))
    assert row["game_id"] == 28 and row["object_key"] == target
    written = s3.calls["put_object"]
    assert provenance.publish(s3, storage, root, "rawg_images") == 1
    assert s3.calls["put_object"] == written


@pytest.mark.parametrize("zone", ["landing", "formatted", "trusted"])
def test_changed_source_or_copy_cannot_acquire_game_association(inputs, s3, zone):
    root, storage, original, target = inputs
    s3.put(zone, original if zone == "landing" else target, b"changed")
    with pytest.raises(ValueError, match="manifest|lineage"):
        provenance.publish(s3, storage, root, "rawg_images")
    assert "provenance/images.jsonl" not in s3.keys("trusted")

"""Unit tests for the per-image quality metrics (no MinIO involved)."""

import hashlib
import io

import numpy as np
import pytest
from PIL import Image, ImageFilter, UnidentifiedImageError

from src.trusted.images import metrics, rules

EXPECTED_KEYS = {
    "width", "height", "aspect_ratio", "file_size", "sharpness", "brightness",
    "contrast", "entropy", "letterbox_ratio", "sha256", "phash",
}


def hamming(first: str, second: str) -> int:
    return bin(int(first, 16) ^ int(second, 16)).count("1")


def test_metrics_expose_exactly_the_fields_the_rules_use(image_bytes):
    assert set(metrics.compute_metrics(image_bytes())) == EXPECTED_KEYS


def test_geometry_and_identity(image_bytes):
    raw = image_bytes(size=(80, 40), color=(10, 20, 30))
    result = metrics.compute_metrics(raw)
    assert (result["width"], result["height"], result["aspect_ratio"]) == (80, 40, 2.0)
    assert result["file_size"] == len(raw)
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()


def test_identical_bytes_give_identical_hashes(image_bytes):
    raw = image_bytes(seed=3)
    assert metrics.compute_metrics(raw)["sha256"] == metrics.compute_metrics(raw)["sha256"]
    assert metrics.compute_metrics(raw)["phash"] == metrics.compute_metrics(raw)["phash"]


@pytest.mark.parametrize("level", [0, 128, 255])
def test_brightness_of_a_solid_gray_image(image_bytes, level):
    result = metrics.compute_metrics(image_bytes(mode="L", color=level))
    assert result["brightness"] == pytest.approx(level)


def test_a_uniform_image_has_no_contrast_no_entropy_and_no_sharpness(image_bytes):
    result = metrics.compute_metrics(image_bytes(color=(90, 90, 90)))
    assert result["contrast"] == 0
    assert result["entropy"] == 0
    assert result["sharpness"] == 0


def test_a_detailed_image_has_contrast_entropy_and_sharpness(image_bytes):
    result = metrics.compute_metrics(image_bytes(seed=1, size=(128, 128)))
    assert result["contrast"] > 30
    assert result["entropy"] > 6
    assert result["sharpness"] > rules.BLUR_THRESHOLD


def test_blurring_lowers_sharpness(image_bytes):
    sharp_raw = image_bytes(seed=1, size=(128, 128))
    blurred = Image.open(io.BytesIO(sharp_raw)).filter(ImageFilter.GaussianBlur(4))
    buffer = io.BytesIO()
    blurred.save(buffer, format="PNG")
    sharp = metrics.compute_metrics(sharp_raw)["sharpness"]
    assert metrics.compute_metrics(buffer.getvalue())["sharpness"] < sharp * 0.5


@pytest.mark.parametrize("side", ["top_bottom", "left_right"])
def test_black_bars_are_measured_as_a_fraction_of_the_image(png_from_array, side):
    pixels = np.full((100, 100), 200, dtype=np.uint8)
    if side == "top_bottom":
        pixels[:10], pixels[90:] = 0, 0
    else:
        pixels[:, :10], pixels[:, 90:] = 0, 0
    assert metrics.compute_metrics(png_from_array(pixels))["letterbox_ratio"] == pytest.approx(0.2)


def test_an_image_without_bars_has_no_letterbox(png_from_array):
    assert metrics.compute_metrics(png_from_array(np.full((50, 50), 200)))["letterbox_ratio"] == 0


def test_an_all_black_image_cannot_exceed_a_ratio_of_one(png_from_array):
    """Regression: dark rows used to be counted from both ends, giving 2.0."""
    assert metrics.compute_metrics(png_from_array(np.zeros((50, 50))))["letterbox_ratio"] == 1


def test_a_recompressed_copy_is_within_the_near_duplicate_distance(image_bytes):
    original = Image.open(io.BytesIO(image_bytes(seed=5, size=(128, 128)))).convert("RGB")
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG", quality=70)
    first = metrics.compute_metrics(image_bytes(seed=5, size=(128, 128)))["phash"]
    second = metrics.compute_metrics(buffer.getvalue())["phash"]
    assert hamming(first, second) <= rules.NEAR_DUP_DISTANCE


def test_unrelated_images_are_far_apart(image_bytes):
    first = metrics.compute_metrics(image_bytes(seed=1, size=(128, 128)))["phash"]
    second = metrics.compute_metrics(image_bytes(seed=2, size=(128, 128)))["phash"]
    assert hamming(first, second) > rules.NEAR_DUP_DISTANCE


def test_undecodable_bytes_raise(image_bytes):
    with pytest.raises(UnidentifiedImageError):
        metrics.compute_metrics(b"not an image")


def test_truncated_file_raises(image_bytes):
    raw = image_bytes(seed=1, size=(200, 200))
    with pytest.raises(OSError):
        metrics.compute_metrics(raw[: len(raw) // 2])
"""Per-image technical metrics used by the Trusted image quality rules."""
from __future__ import annotations

import hashlib
import io

import cv2
import imagehash
import numpy as np
from PIL import Image

DARK_EDGE_THRESHOLD = 10  # mean luminance below this counts as a black bar


def _edge_dark_count(means: np.ndarray, threshold: float = DARK_EDGE_THRESHOLD) -> int:
    """Number of consecutive dark rows/columns from the start of the array."""
    count = 0
    for value in means:
        if value < threshold:
            count += 1
        else:
            break
    return count


def _bar_fraction(means: np.ndarray) -> float:
    """Fraction of rows/columns taken by dark bars at both ends (never above 1)."""
    n = len(means)
    return min(n, _edge_dark_count(means) + _edge_dark_count(means[::-1])) / n


def compute_metrics(raw: bytes) -> dict:
    """Decode an image and return its quality metrics. Raises if it cannot be decoded."""
    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        rgb = img.convert("RGB")

    gray = np.asarray(rgb.convert("L"))
    h, w = gray.shape

    hist = np.bincount(gray.ravel(), minlength=256) / gray.size
    nonzero = hist[hist > 0]
    bars_v = _bar_fraction(gray.mean(axis=1))
    bars_h = _bar_fraction(gray.mean(axis=0))

    return {
        "width": w,
        "height": h,
        "aspect_ratio": round(w / h, 3),
        "file_size": len(raw),
        "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "brightness": float(gray.mean()),
        "contrast": float(gray.std()),
        "entropy": float(-(nonzero * np.log2(nonzero)).sum()),
        "letterbox_ratio": float(max(bars_v, bars_h)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "phash": str(imagehash.phash(rgb)),
    }
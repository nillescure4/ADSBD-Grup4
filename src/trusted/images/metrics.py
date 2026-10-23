import hashlib
import io

import cv2
import imagehash
import numpy as np
from PIL import Image


def _edge_dark_count(means: np.ndarray, threshold: float = 10) -> int:
    """Nombre de files/columnes fosques contigües des del principi."""
    count = 0
    for v in means:
        if v < threshold:
            count += 1
        else:
            break
    return count


def compute_metrics(raw: bytes) -> dict:
    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        rgb = img.convert("RGB")

    gray = np.asarray(rgb.convert("L"))
    h, w = gray.shape

    hist = np.bincount(gray.ravel(), minlength=256) / gray.size
    nonzero = hist[hist > 0]
    row_means, col_means = gray.mean(axis=1), gray.mean(axis=0)
    bars_v = (_edge_dark_count(row_means) + _edge_dark_count(row_means[::-1])) / h
    bars_h = (_edge_dark_count(col_means) + _edge_dark_count(col_means[::-1])) / w

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
"""Quality rules for Trusted images: duplicate detection, soft flags and hard rejections."""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_SIDE = 480            # px
MIN_CONTRAST = 5.0        # luminance std; below this the image is almost uniform
BLUR_THRESHOLD = 50.0     # Laplacian variance
DARK_THRESHOLD = 30.0
BRIGHT_THRESHOLD = 225.0
LETTERBOX_THRESHOLD = 0.10
NEAR_DUP_DISTANCE = 5     # Hamming distance between 64-bit pHashes

RULES_VERSION = 1         # bump whenever a rule or threshold changes meaning
THRESHOLDS = {
    "min_side": MIN_SIDE,
    "min_contrast": MIN_CONTRAST,
    "blur": BLUR_THRESHOLD,
    "dark": DARK_THRESHOLD,
    "bright": BRIGHT_THRESHOLD,
    "letterbox": LETTERBOX_THRESHOLD,
    "near_dup_distance": NEAR_DUP_DISTANCE,
}


METRIC_COLUMNS = ("width", "height", "contrast", "sharpness", "brightness",
                  "letterbox_ratio", "sha256", "phash")


def _with_metric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of df with every metric column present (all-NaN if no image could be decoded)."""
    df = df.copy()
    for column in METRIC_COLUMNS:
        if column not in df.columns:
            df[column] = np.nan
    return df


def _hamming(value: np.uint64, others: np.ndarray) -> np.ndarray:
    xor = np.bitwise_xor(others, value)
    return np.unpackbits(xor.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1)


def flag_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """Flag exact and near duplicates among images that pass the hard rules.

    Rejected images (corrupt, too small, blank) do not take part, so a valid image
    is never discarded as a duplicate of an image that is rejected anyway.
    The first appearance (in key order) is the one that is kept.
    """
    df = _with_metric_columns(df)
    valid = (df["error"].isna() & (df["width"] >= MIN_SIDE)
             & (df["height"] >= MIN_SIDE) & (df["contrast"] >= MIN_CONTRAST))

    # Exact duplicates (same content)
    df["dup_exact"] = False
    df.loc[valid, "dup_exact"] = df[valid].duplicated("sha256", keep="first")

    # Near duplicates (similar pHash)
    df["dup_near"] = False
    candidates = df[valid & ~df["dup_exact"]]
    index = candidates.index.to_numpy()
    hashes = np.array([int(h, 16) for h in candidates["phash"]], dtype=np.uint64)

    kept = np.empty(len(hashes), dtype=np.uint64)
    n_kept = 0
    for pos, h in enumerate(hashes):
        if n_kept and (_hamming(h, kept[:n_kept]) <= NEAR_DUP_DISTANCE).any():
            df.at[index[pos], "dup_near"] = True
        else:
            kept[n_kept] = h
            n_kept += 1
    return df


def apply_rules(df: pd.DataFrame) -> pd.DataFrame:
    df = _with_metric_columns(df)

    # Soft flags (recorded only)
    df["flag_blurry"] = df["sharpness"] < BLUR_THRESHOLD
    df["flag_dark"] = df["brightness"] < DARK_THRESHOLD
    df["flag_bright"] = df["brightness"] > BRIGHT_THRESHOLD
    df["flag_letterbox"] = df["letterbox_ratio"] > LETTERBOX_THRESHOLD

    # Hard rules (reject); the last assignment wins
    df["reject_reason"] = None
    df.loc[df["dup_near"] | df["dup_exact"], "reject_reason"] = "duplicate"
    df.loc[df["contrast"] < MIN_CONTRAST, "reject_reason"] = "blank"
    df.loc[(df["width"] < MIN_SIDE) | (df["height"] < MIN_SIDE), "reject_reason"] = "too_small"
    df.loc[df["error"].notna(), "reject_reason"] = "corrupt"
    return df
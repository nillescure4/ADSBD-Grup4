"""Unit tests for the image quality rules (flags, hard rejections and duplicate detection)."""

import json

import pandas as pd
import pytest

from src.trusted.images import rules

GOOD = {
    "error": None, "width": 800, "height": 600, "contrast": 40.0,
    "sharpness": 200.0, "brightness": 120.0, "letterbox_ratio": 0.0,
}
# 64-bit hashes that are all at least 16 bits apart, so they are never near duplicates
FAR_HASHES = ["0000000000000000", "ffffffffffffffff", "00000000ffffffff",
              "ffffffff00000000", "5555555555555555", "aaaaaaaaaaaaaaaa"]
BASE_HASH = "ffff0000ffff0000"


def flip(hash_hex: str, bits: int, offset: int = 0) -> str:
    """The same hash with `bits` consecutive bits flipped, starting at bit `offset`."""
    mask = ((1 << bits) - 1) << offset
    return format(int(hash_hex, 16) ^ mask, "016x")


def make_df(*overrides: dict) -> pd.DataFrame:
    """One row per override dict; every row is a good, unique image unless told otherwise."""
    rows = []
    for index, changes in enumerate(overrides):
        row = {**GOOD, "key": f"images/{index:03d}.jpg", "sha256": f"sha{index}", "phash": FAR_HASHES[index]}
        row.update(changes)
        rows.append(row)
    return pd.DataFrame(rows)


def evaluate(df: pd.DataFrame) -> pd.DataFrame:
    return rules.apply_rules(rules.flag_duplicates(df))


def reasons(df: pd.DataFrame) -> list:
    return [None if pd.isna(value) else value for value in df["reject_reason"]]


# ---- a clean image ---------------------------------------------------------------------------


def test_a_clean_image_is_accepted_without_flags():
    out = evaluate(make_df({}))
    assert reasons(out) == [None]
    assert not out[["flag_blurry", "flag_dark", "flag_bright", "flag_letterbox", "dup_exact", "dup_near"]].any().any()


def test_the_input_dataframe_is_not_modified():
    df = make_df({}, {})
    evaluate(df)
    assert "dup_exact" not in df.columns and "reject_reason" not in df.columns


# ---- hard rules ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [(480, 480, None), (479, 600, "too_small"), (600, 479, "too_small"), (300, 300, "too_small")],
)
def test_minimum_side_applies_to_both_dimensions(width, height, expected):
    assert reasons(evaluate(make_df({"width": width, "height": height}))) == [expected]


@pytest.mark.parametrize(("contrast", "expected"), [(5.0, None), (4.99, "blank"), (0.0, "blank")])
def test_blank_images_have_almost_no_contrast(contrast, expected):
    assert reasons(evaluate(make_df({"contrast": contrast}))) == [expected]


def test_undecodable_images_are_corrupt_and_do_not_break_the_others():
    df = pd.concat([make_df({}, {}), pd.DataFrame([{"key": "images/bad.jpg", "error": "UnidentifiedImageError: x"}])],
                   ignore_index=True)
    assert reasons(evaluate(df)) == [None, None, "corrupt"]


# ---- soft flags ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "flag", "expected"),
    [
        ({"sharpness": 49.9}, "flag_blurry", True),
        ({"sharpness": 50.0}, "flag_blurry", False),
        ({"brightness": 29.9}, "flag_dark", True),
        ({"brightness": 30.0}, "flag_dark", False),
        ({"brightness": 225.1}, "flag_bright", True),
        ({"brightness": 225.0}, "flag_bright", False),
        ({"letterbox_ratio": 0.11}, "flag_letterbox", True),
        ({"letterbox_ratio": 0.10}, "flag_letterbox", False),
    ],
)
def test_flag_thresholds(changes, flag, expected):
    assert bool(evaluate(make_df(changes))[flag].iloc[0]) is expected


def test_flags_are_recorded_but_never_reject():
    out = evaluate(make_df({"sharpness": 1.0, "brightness": 250.0, "letterbox_ratio": 0.5}))
    assert out[["flag_blurry", "flag_bright", "flag_letterbox"]].all().all()
    assert reasons(out) == [None]


# ---- exact duplicates --------------------------------------------------------------------------------------


def test_exact_duplicates_keep_the_first_occurrence():
    out = evaluate(make_df({"sha256": "same", "phash": BASE_HASH}, {"sha256": "same", "phash": BASE_HASH}))
    assert reasons(out) == [None, "duplicate"]
    assert out["dup_exact"].tolist() == [False, True]
    assert out["dup_near"].tolist() == [False, False]  # already removed as exact, not counted twice


# ---- near duplicates ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bits_different", "expected"),
    [(0, "duplicate"), (rules.NEAR_DUP_DISTANCE, "duplicate"), (rules.NEAR_DUP_DISTANCE + 1, None)],
)
def test_near_duplicate_distance_limit(bits_different, expected):
    out = evaluate(make_df({"phash": BASE_HASH}, {"phash": flip(BASE_HASH, bits_different)}))
    assert reasons(out) == [None, expected]


def test_first_of_several_near_duplicates_is_kept():
    out = evaluate(make_df({"phash": BASE_HASH}, {"phash": flip(BASE_HASH, 2)}, {"phash": flip(BASE_HASH, 3)}))
    assert reasons(out) == [None, "duplicate", "duplicate"]


def test_near_duplicates_are_compared_with_kept_images_only():
    """B is a duplicate of A, and C is close to B but far from A, so C is not chained to B."""
    near_a = flip(BASE_HASH, 5)                       # 5 bits from A
    far_from_a = flip(near_a, 5, offset=5)            # 5 bits from B, 10 bits from A
    out = evaluate(make_df({"phash": BASE_HASH}, {"phash": near_a}, {"phash": far_from_a}))
    assert reasons(out) == [None, "duplicate", None]


def test_a_rejected_twin_does_not_absorb_a_valid_image():
    """Regression: the small copy used to make the big one a duplicate, and both were lost."""
    out = evaluate(make_df({"width": 300, "height": 300, "phash": BASE_HASH}, {"phash": flip(BASE_HASH, 2)}))
    assert reasons(out) == ["too_small", None]
    assert out["dup_near"].tolist() == [False, False]


def test_identical_rejected_images_keep_their_own_reason():
    small = {"width": 300, "height": 300, "sha256": "same", "phash": BASE_HASH}
    out = evaluate(make_df(small, small))
    assert reasons(out) == ["too_small", "too_small"]
    assert not out[["dup_exact", "dup_near"]].any().any()


def test_a_table_where_no_image_could_be_decoded_does_not_crash():
    """Regression: with no metric columns at all the rules raised KeyError."""
    df = pd.DataFrame([{"key": "images/a.jpg", "error": "OSError: x"}, {"key": "images/b.jpg", "error": "OSError: y"}])
    assert reasons(evaluate(df)) == ["corrupt", "corrupt"]


def test_all_images_rejected_does_not_break_duplicate_detection():
    out = evaluate(make_df({"width": 100}, {"contrast": 0.0}))
    assert reasons(out) == ["too_small", "blank"]


# ---- configuration used by the checkpoint ------------------------------------------------------------------


def test_thresholds_match_the_constants_and_serialise_to_json():
    assert rules.THRESHOLDS == {
        "min_side": rules.MIN_SIDE, "min_contrast": rules.MIN_CONTRAST, "blur": rules.BLUR_THRESHOLD,
        "dark": rules.DARK_THRESHOLD, "bright": rules.BRIGHT_THRESHOLD,
        "letterbox": rules.LETTERBOX_THRESHOLD, "near_dup_distance": rules.NEAR_DUP_DISTANCE,
    }
    assert json.loads(json.dumps(rules.THRESHOLDS)) == rules.THRESHOLDS


def test_rules_have_a_positive_integer_version():
    assert isinstance(rules.RULES_VERSION, int) and rules.RULES_VERSION >= 1
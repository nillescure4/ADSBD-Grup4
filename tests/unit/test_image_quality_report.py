"""Unit tests for the Trusted image quality report (figures, tables and review samples)."""

import numpy as np
import pandas as pd
import pytest

from src.trusted.images import quality_report

ROWS = 60
REJECTIONS = ["too_small", "duplicate", "blank"]


def write_quality_csv(path, rows=ROWS, rejected=30, seed=0):
    """A quality table shaped like the one build_trusted_images writes."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "key": [f"images/{index:03d}.jpg" for index in range(rows)],
        "error": None,
        "width": rng.integers(300, 1920, rows),
        "height": rng.integers(300, 1080, rows),
        "file_size": rng.integers(20_000, 400_000, rows),
        "sharpness": rng.uniform(5, 800, rows),
        "brightness": rng.uniform(5, 250, rows),
        "contrast": rng.uniform(10, 80, rows),
        "entropy": rng.uniform(3, 7.8, rows),
        "letterbox_ratio": rng.uniform(0, 0.3, rows),
    })
    df["aspect_ratio"] = (df["width"] / df["height"]).round(3)
    df["sha256"] = [f"sha{index}" for index in range(rows)]
    df["phash"] = [f"{index:016x}" for index in range(rows)]
    df["flag_blurry"] = df["sharpness"] < 50
    df["flag_dark"] = df["brightness"] < 30
    df["flag_bright"] = df["brightness"] > 225
    df["flag_letterbox"] = df["letterbox_ratio"] > 0.10
    df["dup_exact"] = False
    df["dup_near"] = False
    df["reject_reason"] = None
    for index in range(rejected):
        df.loc[index, "reject_reason"] = REJECTIONS[index % len(REJECTIONS)]
    # one undecodable image: it has no metrics at all
    metric_columns = ["width", "height", "file_size", "sharpness", "brightness", "contrast",
                      "entropy", "letterbox_ratio", "aspect_ratio", "sha256", "phash"]
    df[metric_columns] = df[metric_columns].astype(object)
    df.loc[0, metric_columns] = np.nan
    df.loc[0, "error"] = "UnidentifiedImageError: bad file"
    df.loc[0, "reject_reason"] = "corrupt"
    df.to_csv(path, index=False)
    return df


@pytest.fixture
def report(tmp_path):
    csv = tmp_path / "images_quality.csv"
    write_quality_csv(csv)
    out = tmp_path / "report"
    quality_report.main(str(csv), str(out))
    return out


def test_every_metric_has_an_axis_label():
    assert set(quality_report.METRICS) <= set(quality_report.METRIC_LABELS)


def test_all_expected_files_are_generated(report):
    expected = {f"hist_{metric}.png" for metric in quality_report.METRICS}
    expected |= {"outcomes.png", "flag_rates.csv", "metric_summary.csv", "review_samples.csv", "summary.md",
                 "quality_by_resolution_group.csv", "quality_by_brightness_group.csv",
                 "quality_by_aspect_group.csv"}
    assert expected <= {path.name for path in report.iterdir()}


def test_summary_states_the_counts_and_the_caveats(report):
    text = (report / "summary.md").read_text(encoding="utf-8")
    assert "Images measured: 60. Accepted: 30. Rejected: 30." in text
    assert "| corrupt | 1 |" in text
    assert "per image, not per game" in text
    assert "heuristics" in text


def test_metric_summary_ignores_undecodable_images(report):
    summary = pd.read_csv(report / "metric_summary.csv", index_col=0)
    assert summary.loc["count", "width"] == ROWS - 1


def test_review_samples_are_rejected_images_only_and_capped_per_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(quality_report, "SAMPLES_PER_REASON", 3)
    csv = tmp_path / "q.csv"
    table = write_quality_csv(csv)
    quality_report.main(str(csv), str(tmp_path / "out"))
    samples = pd.read_csv(tmp_path / "out" / "review_samples.csv")
    rejected = set(table.loc[table["reject_reason"].notna(), "key"])
    assert set(samples["key"]) <= rejected
    assert samples.groupby("reject_reason").size().max() <= 3
    assert set(samples["reject_reason"]) == set(REJECTIONS) | {"corrupt"}


def test_review_samples_are_reproducible(tmp_path):
    csv = tmp_path / "q.csv"
    write_quality_csv(csv)
    quality_report.main(str(csv), str(tmp_path / "first"))
    quality_report.main(str(csv), str(tmp_path / "second"))
    first = (tmp_path / "first" / "review_samples.csv").read_text()
    assert first == (tmp_path / "second" / "review_samples.csv").read_text()


def test_report_works_when_nothing_was_rejected(tmp_path):
    csv = tmp_path / "q.csv"
    write_quality_csv(csv, rejected=0)
    df = pd.read_csv(csv)
    df.loc[0, ["error", "reject_reason"]] = np.nan  # no corrupt image either
    df = df.iloc[1:]
    df.to_csv(csv, index=False)
    quality_report.main(str(csv), str(tmp_path / "out"))
    assert pd.read_csv(tmp_path / "out" / "review_samples.csv").empty
    assert "Rejected: 0." in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")


def test_group_tables_cover_every_decodable_image(report):
    for name in ("resolution_group", "brightness_group", "aspect_group"):
        table = pd.read_csv(report / f"quality_by_{name}.csv")
        assert table["images"].sum() == ROWS - 1
        assert table["rejected"].between(0, 1).all()


def test_intrinsic_groups_add_the_three_groupings():
    df = pd.DataFrame({"width": [640, 800, 1280, 1920], "height": [480, 450, 720, 1080],
                       "brightness": [10, 100, 150, 240], "aspect_ratio": [0.8, 1.3, 1.78, 2.4]})
    grouped = quality_report.intrinsic_groups(df)
    assert {"resolution_group", "brightness_group", "aspect_group"} <= set(grouped.columns)
    assert grouped["aspect_group"].astype(str).tolist() == ["vertical", "~4:3", "~16:9", "ultra-wide"]


def test_rates_by_reports_the_share_of_flagged_images():
    df = pd.DataFrame({
        "key": list("abcd"), "group": ["x", "x", "y", "y"],
        "rejected": [True, False, False, False], "flag_blurry": [True, True, False, False],
        "flag_dark": [False] * 4, "flag_bright": [False] * 4, "flag_letterbox": [False] * 4,
    })
    rates = quality_report.rates_by(df, "group")
    assert rates.loc["x", "images"] == 2 and rates.loc["x", "rejected"] == 0.5
    assert rates.loc["y", "flag_blurry"] == 0
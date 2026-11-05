#!/usr/bin/env python3
"""Quality report for the Trusted image zone: distributions, rejection rates and review samples."""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless (HPC / no display)
import matplotlib.pyplot as plt
import pandas as pd
from src.trusted.media_review_registry import review_markdown

METRICS = ["width", "height", "aspect_ratio", "sharpness", "brightness",
           "contrast", "entropy", "letterbox_ratio", "file_size"]
METRIC_LABELS = {
    "width": "Width (pixels)",
    "height": "Height (pixels)",
    "aspect_ratio": "Aspect ratio (width / height)",
    "sharpness": "Sharpness (Laplacian variance; higher = sharper)",
    "brightness": "Mean brightness (0 = black, 255 = white)",
    "contrast": "Contrast (luminance std; 0 = uniform image)",
    "entropy": "Entropy of the grayscale histogram (bits)",
    "letterbox_ratio": "Letterbox ratio (fraction covered by black bars)",
    "file_size": "File size (bytes)",
}
RATE_COLS = ["rejected", "flag_blurry", "flag_dark", "flag_bright", "flag_letterbox"]
SAMPLES_PER_REASON = 8


def rates_by(df: pd.DataFrame, col: str) -> pd.DataFrame:
    return df.groupby(col, observed=True).agg(
        images=("key", "size"), **{c: (c, "mean") for c in RATE_COLS}
    ).sort_values("images", ascending=False)


def intrinsic_groups(df: pd.DataFrame) -> pd.DataFrame:
    """Groups derived from the pixels only (no metadata needed)."""
    df = df.copy()
    df["resolution_group"] = pd.qcut(df["width"] * df["height"], 4, duplicates="drop")
    df["brightness_group"] = pd.cut(df["brightness"], [0, 60, 120, 180, 255], include_lowest=True)
    df["aspect_group"] = pd.cut(
        df["aspect_ratio"], [0, 0.99, 1.4, 1.9, 10],
        labels=["vertical", "~4:3", "~16:9", "ultra-wide"],
    )
    return df


def write_summary(q: pd.DataFrame, out: Path) -> None:
    n_rejected = int(q["rejected"].sum())
    outcomes = q["reject_reason"].fillna("accepted").value_counts()
    lines = [
        "# Image quality summary", "",
        f"Images measured: {len(q)}. Accepted: {len(q) - n_rejected}. Rejected: {n_rejected}.", "",
        "| Outcome | Images |", "|---|---:|",
        *[f"| {name} | {count} |" for name, count in outcomes.items()], "",
        "- Statistics are per image, not per game: several screenshots of one game are not independent observations.",
        "- Hard rules (corrupt, too_small, blank, duplicate) remove images; flags (blurry, dark, bright, letterbox) are only recorded.",
        "- Thresholds are heuristics, not validated limits. Check `review_samples.csv` before trusting any of them.",
        "- The tables by group show whether rejection affects some kinds of images more; they do not explain why.", "",
    ]
    lines += review_markdown("image", q["sha256"].dropna())
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main(csv_input: str, output_dir: str) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    q = pd.read_csv(csv_input)
    q["rejected"] = q["reject_reason"].notna()
    ok = q[q["error"].isna()].copy()

    # 1. Technical distributions
    for col in METRICS:
        ax = ok[col].plot.hist(bins=50, title=col)
        ax.set_xlabel(METRIC_LABELS[col])
        ax.set_ylabel("Number of images")
        plt.tight_layout()
        plt.savefig(out / f"hist_{col}.png")
        plt.close()
    ok[METRICS].describe(percentiles=[.05, .25, .5, .75, .95]).to_csv(out / "metric_summary.csv")

    # 2. Overall outcome and flag / duplicate rates
    ax = q["reject_reason"].fillna("accepted").value_counts().plot.bar(title="Quality outcome", rot=0)
    ax.bar_label(ax.containers[0])  # counts on the bars: small categories are hard to read otherwise
    ax.set_xlabel("Outcome of the quality rules")
    ax.set_ylabel("Number of images")
    plt.tight_layout()
    plt.savefig(out / "outcomes.png")
    plt.close()
    flag_cols = [c for c in ok.columns if c.startswith(("flag_", "dup_"))]
    ok[flag_cols].mean().to_csv(out / "flag_rates.csv")

    # 3. Intrinsic fairness: does the filter hit some groups of images harder?
    grouped = intrinsic_groups(ok)
    for col in ["resolution_group", "brightness_group", "aspect_group"]:
        rates_by(grouped, col).to_csv(out / f"quality_by_{col}.csv")

    # 4. Rejection samples for human review: the thresholds are heuristics,
    #    so someone should look at what they reject
    shuffled = q[q["rejected"]].sample(frac=1, random_state=0)
    shuffled.groupby("reject_reason").head(SAMPLES_PER_REASON)[["key", "reject_reason"]].to_csv(
        out / "review_samples.csv", index=False)

    write_summary(q, out)
    print("Report generated in", out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv_input", default="reports/images/images_quality.csv",
                        help="Quality CSV produced by build_trusted_images.py")
    parser.add_argument("--output_dir", default="reports/images/plots",
                        help="Folder for the figures and tables.")
    parser.add_argument("--publish-minio", action="store_true", help="Publish verified report artifacts for the application")
    args = parser.parse_args()
    main(args.csv_input, args.output_dir)
    if args.publish_minio:
        from src.common.config import load_storage_config
        from src.common.minio_client import create_minio_client
        from src.common.quality_reports import publish_bundle
        publish_bundle(create_minio_client(), load_storage_config(), "image", args.csv_input, args.output_dir)

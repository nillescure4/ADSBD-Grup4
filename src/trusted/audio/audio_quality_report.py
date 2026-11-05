"""Summarize audio quality analysis and create reproducible figures from its CSV.

This script reads a precomputed quality report. It does not access or modify MinIO.
"""
from __future__ import annotations

import argparse
import csv
import json
from src.trusted.media_review_registry import review_markdown
import math
import statistics
from collections import Counter
from pathlib import Path

FIELDS = ("duration_seconds", "rms_dbfs", "peak_dbfs", "dc_offset", "silence_ratio")
STATUS_VALUES = {"ok", "review", "invalid", "failed"}


def read_quality_report(path: Path) -> list[dict[str, str]]:
    """Read and validate a complete per-object quality report."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"status", "source_key", "sha256", "duplicate_of", "flags", *FIELDS}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing quality-report columns: {sorted(required - set(reader.fieldnames or []))}")
        rows = list(reader)
    if not rows:
        raise ValueError("Quality report has no rows")
    seen: set[str] = set()
    for row in rows:
        key = row["source_key"]
        if not key or key in seen:
            raise ValueError(f"Empty or duplicated source key: {key!r}")
        if row["status"] not in STATUS_VALUES:
            raise ValueError(f"Unknown quality status for {key}: {row['status']!r}")
        seen.add(key)
    for row in rows:
        if row["duplicate_of"] and row["duplicate_of"] not in seen:
            raise ValueError(f"Missing duplicate representative for {row['source_key']}")
    return rows


def describe(values: list[float]) -> dict[str, float | int | None]:
    """Return robust summary statistics for a series."""
    series = sorted(value for value in values if math.isfinite(value))
    if not series:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    return {"count": len(series), "min": series[0],
            "median": statistics.median(series),
            "p95": series[int(0.95 * (len(series) - 1))], "max": series[-1]}


def summarize(rows: list[dict[str, str]]) -> dict:
    """Compute counts and ranges without treating advisory flags as failures."""
    statuses = Counter(row["status"] for row in rows)
    flags = Counter(flag for row in rows for flag in row["flags"].split(";") if flag)
    measurements: dict[str, list[float]] = {}
    for name in FIELDS:
        measurements[name] = []
        for row in rows:
            if not row[name]:
                continue
            try:
                measurements[name].append(float(row[name]))
            except ValueError:
                continue
    dcs = measurements["dc_offset"]
    return {
        "total": len(rows),
        "status_counts": {status: statuses[status] for status in sorted(STATUS_VALUES)},
        "flag_counts": dict(sorted(flags.items())),
        "duplicate_files": sum(bool(row["duplicate_of"]) for row in rows),
        "unique_file_candidates": len(rows) - sum(bool(row["duplicate_of"]) for row in rows),
        "dc_offset_over_0_05": sum(abs(x) > 0.05 for x in dcs),
        "metrics": {name: describe(values) for name, values in measurements.items()},
        "total_duration_hours": sum(x for x in measurements["duration_seconds"] if math.isfinite(x)) / 3600,
    }


def create_figures(rows: list[dict[str, str]], output_dir: Path) -> list[Path]:
    """Save one figure per quality dimension for the project report."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    produced: list[Path] = []
    figure_specs = [
        ("duration_seconds", "Duration (seconds)", "duration_histogram.png", True),
        ("rms_dbfs", "RMS level (dBFS)", "rms_histogram.png", False),
        ("peak_dbfs", "Peak level (dBFS)", "peak_histogram.png", False),
        ("dc_offset", "DC offset (absolute sample units)", "dc_offset_histogram.png", False),
    ]
    for field, label, filename, trim_outliers in figure_specs:
        values = []
        for row in rows:
            try:
                number = float(row[field])
            except (ValueError, TypeError):
                continue
            if math.isfinite(number):
                values.append(number)
        if field == "dc_offset":
            values = [abs(v) for v in values]
        fig, ax = plt.subplots(figsize=(9, 4.8))
        if values:
            if trim_outliers and len(values) > 1:
                upper = sorted(values)[int(0.99 * (len(values) - 1))]
                visible = [value for value in values if value <= upper]
                label += " (up to 99th percentile)"
            else:
                visible = values
            ax.hist(visible, bins=40, edgecolor="white", linewidth=0.5)
        ax.set(xlabel=label, ylabel="Number of audio files", title=field.replace("_", " ").title())
        fig.tight_layout()
        dest = output_dir / filename
        fig.savefig(dest, dpi=160)
        plt.close(fig)
        produced.append(dest)

    flags = Counter(flag for row in rows for flag in row["flags"].split(";") if flag)
    fig, ax = plt.subplots(figsize=(9, 4.8))
    if flags:
        labels, counts = zip(*sorted(flags.items(), key=lambda item: item[1]))
        ax.barh(labels, counts)
        for index, count in enumerate(counts):
            ax.text(count, index, f" {count}", va="center")
        ax.set_xlim(right=max(counts) * 1.18)
    ax.set(xlabel="Files flagged (categories may overlap)", title="Quality indicators")
    fig.tight_layout()
    dest = output_dir / "quality_flags.png"
    fig.savefig(dest, dpi=160)
    plt.close(fig)
    produced.append(dest)
    return produced


def make_quality_report(source: Path, output_dir: Path) -> dict:
    """Generate numerical summaries, duplicate mapping, plots, and a brief Markdown report."""
    rows = read_quality_report(source)
    summary = summarize(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "quality_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "exact_duplicates.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["duplicate_key", "representative_key", "sha256"])
        writer.writeheader()
        for row in rows:
            if row["duplicate_of"]:
                writer.writerow({"duplicate_key": row["source_key"],
                                 "representative_key": row["duplicate_of"],
                                 "sha256": row["sha256"]})
    create_figures(rows, output_dir)
    status = summary["status_counts"]
    flags = summary["flag_counts"]
    lines = [
        "# Audio quality profiling — before Trusted cleaning", "",
        f"Source CSV: `{source.name}`", "",
        f"- Inspected files: {summary['total']}",
        f"- OK: {status['ok']}; review: {status['review']}; invalid: {status['invalid']}; failed: {status['failed']}",
        f"- Byte-identical duplicates: {summary['duplicate_files']}",
        f"- Unique-content candidates: {summary['unique_file_candidates']}",
        f"- Files with |DC offset| > 0.05: {summary['dc_offset_over_0_05']}",
        f"- Total audio duration (hours): {summary['total_duration_hours']:.2f}", "",
        "## Quality flags", "",
    ]
    lines += [f"- {name}: {count}" for name, count in flags.items()]
    lines += ["", "## Metric medians", ""]
    for metric, stats in summary["metrics"].items():
        value = stats["median"]
        lines.append(f"- {metric}: {value:.4f}" if value is not None else f"- {metric}: unavailable")
    lines += [
        "", "## Interpretation", "",
        "Review flags are advisory and must not automatically trigger deletion.",
        "Near-full-scale audio does not necessarily indicate clipping.",
        "Exact duplicates are identified from FLAC file bytes, not perceptual similarity.",
        "DC correction requires independent post-conversion verification and headroom checks.",
        "", "## Figures", "",
        "- duration_histogram.png", "- rms_histogram.png", "- peak_histogram.png",
        "- dc_offset_histogram.png", "- quality_flags.png", "",
    ]
    lines += review_markdown("audio", [row["sha256"] for row in rows])
    (output_dir / "quality_overview.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main() -> int:
    """Create the quality summary and plots from the CSV report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("reports/audio_trusted_quality_full.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/audio_trusted_quality_analysis"))
    parser.add_argument("--publish-minio", action="store_true", help="Publish verified report artifacts for the application")
    parser.add_argument("--processing-report", type=Path, help="Trusted processing outcomes to include with the report")
    args = parser.parse_args()
    try:
        result = make_quality_report(args.report, args.output_dir)
        if args.publish_minio:
            from src.common.config import load_storage_config
            from src.common.minio_client import create_minio_client
            from src.common.quality_reports import publish_bundle
            publish_bundle(create_minio_client(), load_storage_config(), "audio", args.report, args.output_dir, args.processing_report)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Analysis created in {args.output_dir}: total={result['total']}, "
          f"duplicates={result['duplicate_files']}, review={result['status_counts']['review']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

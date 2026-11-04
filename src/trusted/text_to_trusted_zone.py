"""Incrementally analyse and clean Formatted text Parquet partitions."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client, list_object_keys
from src.ingestion.text_temporal_to_persistent import read_bytes, read_optional
from src.trusted.text_quality import QUALITY_VERSION, assess_and_clean
from src.trusted.text_quality_analysis import analysis_markdown, overview_report
from src.trusted.text_review_registry import registry_digest


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def quality_markdown(report: dict[str, Any]) -> str:
    before, after, corrections = report["before"], report["after"], report["corrections"]
    lines = [
        f"# RAWG text quality: {report['collection_date']}", "",
        f"Analysed sample: {before['rows']} captures for this UTC date. "
        "Results describe this partition and do not generalise to all RAWG data.", "",
        "| Indicator | Before | After |", "|---|---:|---:|",
        f"| Rows | {before['rows']} | {after['rows']} |",
        f"| Missing text (null or empty) | {before['selected_text']['null'] + before['selected_text']['empty_or_whitespace']} | {after['description_clean']['null'] + after['description_clean']['empty_or_whitespace']} |",
        f"| Text with detectable HTML | {before['selected_text']['html']} | {after['description_clean']['html']} |",
        f"| Text with control characters | {before['selected_text']['control_characters']} | {after['description_clean']['control_characters']} |",
        f"| Nonempty text below advisory threshold | {before['selected_text']['short_nonempty']} | {after['description_clean']['short_nonempty']} |", "",
        "## Corrections", "",
        f"- Exact duplicates removed: {corrections['exact_duplicates_removed']}.",
        f"- Texts changed in the clean column: {corrections['text_changed_rows']}.",
        f"- Texts recovered from description as a fallback: {corrections['html_fallback_rows']}.",
        "- Parquet retains description_clean as its only description column. "
        "Other business columns and captures from other dates are retained. "
        "Provenance is recorded in manifests and reports rather than Parquet columns.",
        "- Original descriptions remain available in Formatted and Landing; "
        "diagnostics are stored only in reports.", "",
        "## Quality issues", "",
        f"Rows with warnings: {after['rows_with_warnings']}. Warnings do not remove rows.",
    ]
    lines.extend(f"- {flag}: {count}." for flag, count in after["flag_counts"].items())
    if not after["flag_counts"]:
        lines.append("No issues were detected by the implemented rules.")
    if report["issues"]:
        lines += ["", "### Rows with quality issues", "",
                  "The row numbers below start at 1 and exclude any header.", ""]
        for issue in report["issues"]:
            lines.append(
                f"- Trusted row {issue['output_row'] + 1} (input {issue['input_row'] + 1}); "
                f"ID {issue['id']}; date {issue['collection_date']}; "
                f"source `{issue['source_object_key']}`; warnings: {', '.join(issue['flags'])}."
            )
    lines += ["", "## Criteria and limitations", "",
              f"The {report['rules']['min_description_chars']}-character threshold only raises a warning; it does not determine text validity by itself.",
              "Checks cover identifiers, dates, provenance and JSON field syntax/structure. "
              "Cleaning removes HTML and script/style content and normalises whitespace and control characters. "
              "No values are imputed, translated or case-folded; accents are retained and no embeddings are generated here. "
              "Unconfirmed numerical ranges and speculative spelling corrections are not applied.", "",
              "The accompanying JSON includes null counts by column, length statistics and per-row analysis "
              "with ID, date, source key, selected description field, length and warnings "
              "(row indices start at 0). "
              "Deduplication and description comparisons are scoped to this date; "
              "captures of the same game on different dates are retained.", ""]
    return "\n".join(lines) + analysis_markdown(report)


def update_overview(client: Any, bucket: str, prefix: str, partitions: list[dict[str, str]]) -> None:
    """Build an aggregate only from verified partitions of the current run."""
    checkpoint_key = f"{prefix}manifests/quality_overview.json"
    signature = {"quality_version": QUALITY_VERSION, "partitions": partitions}
    old_bytes = read_optional(client, bucket, checkpoint_key)
    old = json.loads(old_bytes) if old_bytes else None
    if old and old.get("input") == signature and len(old.get("outputs", {})) == 2:
        if all((current := read_optional(client, bucket, key)) is not None and digest(current) == expected
               for key, expected in old["outputs"].items()):
            print("[SKIP] Global quality overview is already up to date")
            return
    datasets = []
    for partition in partitions:
        checkpoint_bytes = read_bytes(client, bucket, partition["checkpoint_key"])
        if digest(checkpoint_bytes) != partition["checkpoint_sha256"]:
            raise ValueError("A partition changed during global analysis")
        checkpoint = json.loads(checkpoint_bytes)
        day = partition["date"]
        report_key = f"{prefix}quality/collection_date={day}.json"
        output_key = f"{prefix}collection_date={day}/game_details.parquet"
        report_bytes = read_bytes(client, bucket, report_key)
        data = read_bytes(client, bucket, output_key)
        if digest(report_bytes) != checkpoint["outputs"][report_key] or digest(data) != checkpoint["outputs"][output_key]:
            raise ValueError("Data or a report changed during global analysis")
        datasets.append((json.loads(report_bytes), pq.ParquetFile(pa.BufferReader(data)).read()))
    report = overview_report(datasets)
    report["quality_version"] = QUALITY_VERSION
    report["provenance"] = signature
    lines = ["# Global RAWG text quality analysis", "",
             f"Captures: {report['capture_count']}. Distinct games with valid IDs: {report['distinct_valid_game_ids']}. "
             f"Dates: {', '.join(report['collection_dates']) or 'none'}.", "",
             f"Rows with warnings: {report['warnings']}. Exact duplicates removed: {report['exact_duplicates_removed']}.", "",
             "## Dates and recurring issues", "", "| Date | Trusted captures | Rows with warnings |", "|---|---:|---:|"]
    for partition in report["partitions"]:
        lines.append(f"| {partition['date']} | {partition['rows']} | {partition['warnings']} |")
    for flag, counts in report["recurring_flags"].items():
        lines.append(f"- {flag}: {counts['row_count']} rows across {counts['dates_count']} dates.")
    markdown = "\n".join(lines) + "\n" + analysis_markdown(report, global_report=True)
    outputs = {
        f"{prefix}quality/overview.json": (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), "application/json"),
        f"{prefix}quality/overview.md": (markdown.encode("utf-8"), "text/markdown; charset=utf-8"),
    }
    for key, (body, content_type) in outputs.items():
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        if read_bytes(client, bucket, key) != body:
            raise ValueError(f"Could not verify {key}")
    checkpoint = {"input": signature, "outputs": {key: digest(body) for key, (body, _) in outputs.items()}}
    body = json.dumps(checkpoint, ensure_ascii=False, indent=2).encode("utf-8")
    client.put_object(Bucket=bucket, Key=checkpoint_key, Body=body, ContentType="application/json")
    if read_bytes(client, bucket, checkpoint_key) != body:
        raise ValueError("Could not verify the global overview checkpoint")
    print(f"[OK] Global overview: {report['capture_count']} captures, {report['distinct_valid_game_ids']} distinct games")


def trust_texts(*, dry_run: bool = False, min_description_chars: int = 50) -> int:
    if min_description_chars < 1:
        raise ValueError("The length threshold must be positive")
    storage = load_storage_config()
    source_bucket, target_bucket = storage["buckets"]["formatted"], storage["buckets"]["trusted"]
    source_prefix = f"{storage['formatted']['texts_prefix'].strip('/')}/rawg/"
    target_prefix = f"{storage['trusted']['texts_prefix'].strip('/')}/rawg/"
    client = create_minio_client()
    client.head_bucket(Bucket=target_bucket)
    pattern = re.compile(r"collection_date=(\d{4}-\d{2}-\d{2})/game_details\.parquet")
    written = skipped = errors = 0
    partitions = []
    for key in sorted(list_object_keys(client, source_bucket, source_prefix)):
        match = pattern.fullmatch(key[len(source_prefix):])
        if not match:
            continue
        day = match[1]
        checkpoint_key = f"{target_prefix}manifests/collection_date={day}.json"
        try:
            raw = read_bytes(client, source_bucket, key)
            source_checkpoint_key = f"{source_prefix}manifests/collection_date={day}.json"
            source_checkpoint_bytes = read_bytes(client, source_bucket, source_checkpoint_key)
            source_checkpoint = json.loads(source_checkpoint_bytes)
            if source_checkpoint["output_key"] != key or source_checkpoint["output_sha256"] != digest(raw):
                raise ValueError("The Formatted Parquet does not match its checkpoint")
            signature = {"quality_version": QUALITY_VERSION, "min_description_chars": min_description_chars,
                         "review_registry_sha256": registry_digest(),
                         "source_bucket": source_bucket, "source_key": key, "source_sha256": digest(raw),
                         "source_manifest_sha256": digest(source_checkpoint_bytes)}
            old_bytes = read_optional(client, target_bucket, checkpoint_key)
            old = json.loads(old_bytes) if old_bytes else None
            if old and old.get("input") == signature:
                valid = True
                for output_key, expected_hash in old["outputs"].items():
                    current = read_optional(client, target_bucket, output_key)
                    if current is None or digest(current) != expected_hash:
                        valid = False
                        break
                if valid and len(old["outputs"]) == 3:
                    skipped += 1
                    partitions.append({"date": day, "checkpoint_key": checkpoint_key, "checkpoint_sha256": digest(old_bytes)})
                    print(f"[SKIP] {day}: Trusted data and reports are already up to date")
                    continue

            table = pq.ParquetFile(pa.BufferReader(raw)).read()
            if source_checkpoint["row_count"] != table.num_rows:
                raise ValueError("The Formatted row count does not match its checkpoint")
            cleaned, report = assess_and_clean(
                table, day, min_description_chars=min_description_chars,
                row_provenance=source_checkpoint["row_provenance"],
                expected_sources={item["key"]: item["sha256"] for item in source_checkpoint["inputs"]},
            )
            report["provenance"] = signature
            if dry_run:
                print(f"[DRY] {day}: {table.num_rows} -> {cleaned.num_rows} rows, {report['after']['rows_with_warnings']} with warnings")
                continue
            buffer = pa.BufferOutputStream()
            pq.write_table(cleaned, buffer, compression="snappy")
            output_key = f"{target_prefix}collection_date={day}/game_details.parquet"
            outputs = {
                output_key: (buffer.getvalue().to_pybytes(), "application/vnd.apache.parquet"),
                f"{target_prefix}quality/collection_date={day}.json": (json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"),
                f"{target_prefix}quality/collection_date={day}.md": (quality_markdown(report).encode("utf-8"), "text/markdown; charset=utf-8"),
            }
            for output, (body, content_type) in outputs.items():
                client.put_object(Bucket=target_bucket, Key=output, Body=body, ContentType=content_type)
                stored = read_bytes(client, target_bucket, output)
                if stored != body:
                    raise ValueError(f"Could not verify {output}")
                if output == output_key:
                    restored = pq.ParquetFile(pa.BufferReader(stored)).read()
                    # Arrow equality treats NaN as unequal; preserve and flag it instead.
                    if (restored.schema != cleaned.schema or
                        json.dumps(restored.to_pylist(), sort_keys=True) != json.dumps(cleaned.to_pylist(), sort_keys=True)):
                        raise ValueError("The Trusted Parquet does not match the clean table")
            checkpoint = {"input": signature, "outputs": {output: digest(body) for output, (body, _) in outputs.items()},
                          "rows": cleaned.num_rows, "warnings": report["after"]["rows_with_warnings"]}
            checkpoint_body = json.dumps(checkpoint, ensure_ascii=False, indent=2).encode("utf-8")
            client.put_object(Bucket=target_bucket, Key=checkpoint_key, Body=checkpoint_body, ContentType="application/json")
            if read_bytes(client, target_bucket, checkpoint_key) != checkpoint_body:
                raise ValueError("Could not verify the Trusted checkpoint")
            written += 1
            partitions.append({"date": day, "checkpoint_key": checkpoint_key, "checkpoint_sha256": digest(checkpoint_body)})
            print(f"[OK] {day}: {cleaned.num_rows} rows, {cleaned.num_columns} columns, {report['after']['rows_with_warnings']} with warnings")
        except Exception as exc:
            errors += 1
            print(f"[ERROR] {day}: {exc}", file=sys.stderr)
    if not dry_run and not errors:
        try:
            update_overview(client, target_bucket, target_prefix, partitions)
        except Exception as exc:
            errors += 1
            print(f"[ERROR] Global overview: {exc}", file=sys.stderr)
    elif errors:
        print("[WARN] The global overview is not updated because a partial analysis must not be presented as complete.")
    print(f"Trusted: {written} partitions written, {skipped} skipped, {errors} errors.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Analyse without writing to MinIO.")
    parser.add_argument("--min-description-chars", type=int, default=50, help="Advisory threshold for short-text warnings; rows are not removed.")
    args = parser.parse_args()
    if args.min_description_chars < 1:
        parser.error("--min-description-chars must be positive")
    raise SystemExit(trust_texts(dry_run=args.dry_run, min_description_chars=args.min_description_chars))


if __name__ == "__main__":
    main()

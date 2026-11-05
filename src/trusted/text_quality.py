"""Generic quality rules and profiling for RAWG text in Parquet tables."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
import math
import re
import unicodedata
from typing import Any

import pyarrow as pa

from src.trusted.text_quality_analysis import (
    completeness_profile, interpretations, numeric_columns, numeric_profile,
    review_sample, select_review_samples,
)
from src.trusted.text_review_registry import review_summary


QUALITY_VERSION = 5
PROVENANCE_FIELDS = {"source", "ingested_at_utc", "source_object_key", "source_sha256"}
REMOVED_DESCRIPTION_FIELDS = {"description", "description_raw"}
HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
ADDED_FIELDS = {
    "description_clean", "description_clean_source", "description_length",
    "quality_flags", "quality_status",
}


class TextExtractor(HTMLParser):
    """Preserve block boundaries and omit script/style content."""

    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden.append(tag)
        if tag in self.BLOCKS and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_description(value: str, *, html: bool = False) -> str:
    if html or HTML_TAG.search(value):
        parser = TextExtractor()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    else:
        value = unescape(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(char for char in value if char in "\n\t" or unicodedata.category(char) != "Cc")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def collection_date(row: dict[str, Any]) -> str | None:
    try:
        stamp = datetime.fromisoformat(row["ingested_at_utc"])
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc).date().isoformat()
    except (ValueError, TypeError, KeyError):
        return None


def text_profile(values: list[Any], minimum: int) -> dict[str, Any]:
    lengths = [len(value) for value in values if isinstance(value, str) and value.strip()]
    return {
        "null": sum(value is None for value in values),
        "empty_or_whitespace": sum(isinstance(value, str) and not value.strip() for value in values),
        "non_text": sum(value is not None and not isinstance(value, str) for value in values),
        "short_nonempty": sum(0 < length < minimum for length in lengths),
        "html": sum(bool(HTML_TAG.search(value)) for value in values if isinstance(value, str)),
        "control_characters": sum(any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in value) for value in values if isinstance(value, str)),
        "length": {"min": min(lengths) if lengths else None, "max": max(lengths) if lengths else None,
                   "mean": sum(lengths) / len(lengths) if lengths else None},
    }


def assess_and_clean(table: pa.Table, day: str, *, min_description_chars: int = 50,
                     row_provenance: list[dict[str, Any]],
                     expected_sources: dict[str, str] | None = None) -> tuple[pa.Table, dict[str, Any]]:
    """Output only clean description; keep diagnostics in the separate report."""
    date.fromisoformat(day)
    if min_description_chars < 1:
        raise ValueError("The length threshold must be positive")
    required = {"id", "description", "description_raw"}
    if required - set(table.column_names):
        raise ValueError(f"Missing required columns: {sorted(required - set(table.column_names))}")
    if ADDED_FIELDS & set(table.column_names):
        raise ValueError("The input already contains Trusted columns")
    if len(row_provenance) != table.num_rows:
        raise ValueError("Manifest provenance does not cover every row")
    rows = []
    for index, (row, provenance) in enumerate(zip(table.to_pylist(), row_provenance)):
        if provenance.get("row_index") != index or PROVENANCE_FIELDS - provenance.keys():
            raise ValueError("Invalid provenance fields or row index in the manifest")
        if provenance.get("id") != row["id"]:
            raise ValueError("The row ID does not match the manifest")
        rows.append({**row, **{name: provenance[name] for name in PROVENANCE_FIELDS}})
    unique = []
    seen = set()
    duplicate_indices = []
    for index, row in enumerate(rows):
        signature = json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=True)
        if signature in seen:
            duplicate_indices.append(index)
        else:
            seen.add(signature)
            unique.append((index, row))

    json_fields = set(json.loads((table.schema.metadata or {}).get(b"json_encoded_fields", b"[]")))
    capture_counts = Counter((str(row["id"]), collection_date(row)) for _, row in unique)
    prepared = []
    same_text: dict[str, set[int]] = defaultdict(set)
    for index, row in unique:
        flags = []
        game_id = row["id"]
        collected_day = collection_date(row)
        if type(game_id) is not int or game_id <= 0:
            flags.append("invalid_id")
        if collected_day is None:
            flags.append("invalid_collection_timestamp")
        elif collected_day != day:
            flags.append("collection_date_mismatch")
        if capture_counts[str(game_id), collected_day] > 1:
            flags.append("conflicting_capture")
        if row.get("source") != "rawg":
            flags.append("unexpected_source")
        if not isinstance(row.get("source_object_key"), str) or not row["source_object_key"]:
            flags.append("missing_source_object_key")
        if not isinstance(row.get("source_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"]):
            flags.append("invalid_source_sha256")
        if expected_sources is not None and expected_sources.get(row.get("source_object_key")) != row.get("source_sha256"):
            flags.append("provenance_not_in_formatted_manifest")
        for field, value in row.items():
            if isinstance(value, float) and not math.isfinite(value):
                flags.append(f"nonfinite_number:{field}")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            flags.append("missing_name")
        released = row.get("released")
        if released is not None:
            try:
                date.fromisoformat(released)
            except (ValueError, TypeError):
                flags.append("invalid_release_date")
        if row.get("updated") is not None:
            try:
                datetime.fromisoformat(row["updated"])
            except (ValueError, TypeError):
                flags.append("invalid_updated_timestamp")
        for field in sorted(json_fields):
            if row.get(field) is not None:
                try:
                    decoded = json.loads(row[field])
                    if field in {"genres", "platforms", "tags", "developers", "publishers", "stores", "ratings", "parent_platforms", "metacritic_platforms", "alternative_names"} and not isinstance(decoded, list):
                        flags.append(f"invalid_json_structure:{field}")
                except (ValueError, TypeError):
                    flags.append(f"invalid_json:{field}")

        cleaned = None
        selected = None
        for field in ("description_raw", "description"):
            value = row.get(field)
            if value is not None and not isinstance(value, str):
                flags.append(f"non_text:{field}")
            if isinstance(value, str) and value.strip():
                candidate = clean_description(value, html=field == "description")
                if candidate:
                    cleaned, selected = candidate, field
                    break
        if cleaned is None:
            flags.append("missing_description")
        else:
            if len(cleaned) < min_description_chars:
                flags.append("short_description")
            if HTML_TAG.search(cleaned):
                flags.append("residual_html")
            if type(game_id) is int:
                same_text[cleaned].add(game_id)
        prepared.append((index, row, cleaned, selected, flags))

    flag_counts: Counter[str] = Counter()
    issues = []
    row_analysis = []
    output_rows = []
    for index, row, cleaned, selected, flags in prepared:
        if cleaned and len(same_text[cleaned]) > 1:
            flags.append("shared_description_across_games")
        flags = sorted(set(flags))
        flag_counts.update(flags)
        analysis = {
            "input_row": index, "output_row": len(output_rows),
            "id": row["id"], "collection_date": day, "ingested_at_utc": row["ingested_at_utc"],
            "source_object_key": row["source_object_key"],
            "source": row["source"], "source_sha256": row["source_sha256"],
            "description_clean_source": selected,
            "description_length": len(cleaned) if cleaned is not None else None,
            "flags": flags, "status": "warning" if flags else "ok",
        }
        row_analysis.append(analysis)
        if flags:
            issues.append(analysis)
        output_rows.append({**{name: row[name] for name in table.column_names if name not in REMOVED_DESCRIPTION_FIELDS},
                            "description_clean": cleaned})

    schema = pa.schema([field for field in table.schema if field.name not in REMOVED_DESCRIPTION_FIELDS],
                       metadata=table.schema.metadata).append(pa.field("description_clean", pa.string()))
    metadata = dict(schema.metadata or {})
    metadata[b"quality_version"] = str(QUALITY_VERSION).encode()
    if b"json_encoded_fields" in metadata:
        metadata[b"json_encoded_fields"] = json.dumps(sorted(json_fields - REMOVED_DESCRIPTION_FIELDS)).encode()
    schema = schema.with_metadata(metadata)
    output = pa.Table.from_pylist(output_rows, schema=schema)
    source_texts = [row["description_raw"] if isinstance(row["description_raw"], str) and row["description_raw"].strip() else row["description"] for row in rows]
    report = {
        "quality_version": QUALITY_VERSION, "collection_date": day,
        "rules": {"min_description_chars": min_description_chars, "short_text_action": "flag_only",
                  "deduplication": "exact_full_row_only_within_partition", "original_columns": "preserved_except_description_and_description_raw",
                  "diagnostic_columns": "report_only",
                  "range_rules": "no_unverified_rating_ranges_or_automatic_imputation"},
        "before": {"rows": len(rows), "nulls_by_column": {name: sum(row[name] is None for row in rows) for name in table.column_names},
                   "description_raw": text_profile([row["description_raw"] for row in rows], min_description_chars),
                   "description": text_profile([row["description"] for row in rows], min_description_chars),
                   "selected_text": text_profile(source_texts, min_description_chars)},
        "after": {"rows": len(output_rows), "nulls_by_column": {name: sum(row[name] is None for row in output_rows) for name in output.column_names},
                  "description_clean": text_profile([row["description_clean"] for row in output_rows], min_description_chars),
                  "rows_with_warnings": len(issues), "flag_counts": dict(sorted(flag_counts.items()))},
        "corrections": {"exact_duplicates_removed": len(duplicate_indices), "removed_input_rows": duplicate_indices,
                        "html_fallback_rows": sum(selected == "description" for _, _, _, selected, _ in prepared),
                        "text_changed_rows": sum(cleaned is not None and cleaned != row[selected] for _, row, cleaned, selected, _ in prepared)},
        "shared_description_groups": sum(len(ids) > 1 for ids in same_text.values()), "issues": issues,
        "row_index_base": 0, "row_analysis": row_analysis,
    }
    numbers = numeric_columns(table)
    report["before"]["numeric_profile"] = numeric_profile(rows, numbers)
    report["after"]["numeric_profile"] = numeric_profile(output_rows, numbers)
    report["before"]["completeness"] = completeness_profile(rows, table.column_names, json_fields)
    report["after"]["completeness"] = completeness_profile(output_rows, output.column_names, json_fields)
    samples = [review_sample({**original, "description_clean": cleaned_row["description_clean"]}, analysis)
               for (_, original), cleaned_row, analysis in zip(unique, output_rows, row_analysis)]
    report["qualitative_review"] = {
        **review_summary(select_review_samples(samples)), "selection": "signals_then_changed_then_other_distinct_games_first",
        "source": "full_text_compared_for_lengths_and_signals; excerpts_max_2000_characters",
        "rows_with_review_signals": sum(bool(sample["checks"]) for sample in samples),
        "samples": select_review_samples(samples),
    }
    report["interpretation"] = [
        f"Analysed {len(rows)} captures for this date and retained {len(output_rows)}. "
        "This sample cannot certify semantic quality or justify generalising to the full catalogue.",
        *interpretations(report["after"]["completeness"], len(output_rows)),
    ]
    return output, report

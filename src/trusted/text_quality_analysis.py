"""Perfils descriptius i informes de qualitat; no transforma les dades."""

from __future__ import annotations

from collections import Counter
import json
import math
import re
import statistics
from typing import Any

import pyarrow as pa


def percentile(values: list[int | float], fraction: float) -> int | float | None:
    if not values:
        return None
    position = (len(values) - 1) * fraction
    left, right = math.floor(position), math.ceil(position)
    return values[left] if left == right else values[left] + (values[right] - values[left]) * (position - left)


def numeric_profile(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, Any]:
    profiles = {}
    for name in sorted(columns):
        raw = [row.get(name) for row in rows]
        finite = sorted(value for value in raw if type(value) in (int, float) and math.isfinite(value))
        n = len(finite)
        q1, q3 = percentile(finite, 0.25), percentile(finite, 0.75)
        identifier = name == "id"
        lower = q1 - 1.5 * (q3 - q1) if n >= 4 and not identifier else None
        upper = q3 + 1.5 * (q3 - q1) if n >= 4 and not identifier else None
        histogram = []
        if finite:
            if finite[0] == finite[-1]:
                histogram = [{"lower": finite[0], "upper": finite[-1], "count": n}]
            else:
                width = (finite[-1] - finite[0]) / 5
                counts = [0] * 5
                for value in finite:
                    counts[min(4, int((value - finite[0]) / width))] += 1
                histogram = [{"lower": finite[0] + width * index,
                              "upper": finite[-1] if index == 4 else finite[0] + width * (index + 1),
                              "count": count} for index, count in enumerate(counts)]
        profiles[name] = {
            "role": "identifier" if identifier else "measure",
            "count": len(raw), "null_count": sum(value is None for value in raw),
            "null_pct": 100 * sum(value is None for value in raw) / len(raw) if raw else 0,
            "nonfinite_count": sum(type(value) is float and not math.isfinite(value) for value in raw),
            "finite_count": n, "unique_finite_values": len(set(finite)),
            "min": finite[0] if n else None, "max": finite[-1] if n else None,
            "mean": statistics.fmean(finite) if n else None,
            "median": percentile(finite, 0.5), "q1": q1, "q3": q3,
            "p95": percentile(finite, 0.95), "histogram": histogram,
            "histogram_intervals": "left_closed_right_open_except_last_closed",
            "potential_extremes": {
                "method": "1.5_IQR_descriptive_only_minimum_4_finite_values",
                "lower": lower, "upper": upper,
                "count": sum(value < lower or value > upper for value in finite) if lower is not None else None,
                "action": "none; identifiers excluded; no removal or imputation",
            },
        }
    return profiles


def numeric_columns(table: pa.Table) -> list[str]:
    return [field.name for field in table.schema if pa.types.is_integer(field.type) or pa.types.is_floating(field.type)]


def completeness_profile(rows: list[dict[str, Any]], columns: list[str], json_fields: set[str]) -> dict[str, Any]:
    profiles = {}
    for name in columns:
        values = [row.get(name) for row in rows]
        nulls = sum(value is None for value in values)
        empty = sum(isinstance(value, str) and not value.strip() for value in values)
        containers = 0
        if name in json_fields:
            for value in values:
                if isinstance(value, str):
                    try:
                        decoded = json.loads(value)
                        containers += isinstance(decoded, (list, dict)) and not decoded
                    except ValueError:
                        pass
        profiles[name] = {"count": len(rows), "null_count": nulls, "empty_text_count": empty,
                          "empty_json_container_count": containers,
                          "missing_count": nulls + empty,
                          "missing_pct": 100 * (nulls + empty) / len(rows) if rows else 0}
    return profiles


def review_sample(row: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    field = analysis["description_clean_source"]
    original = row.get(field) if field else None
    comparison_field = field
    if original is None:
        comparison_field = next((name for name in ("description_raw", "description")
                                 if isinstance(row.get(name), str) and row[name].strip()), None)
        original = row.get(comparison_field) if comparison_field else None
    cleaned = row.get("description_clean")
    original_length = len(original) if isinstance(original, str) else 0
    clean_length = len(cleaned) if isinstance(cleaned, str) else 0
    reduction = 100 * (original_length - clean_length) / original_length if original_length else None
    checks = []
    if isinstance(original, str) and original.strip() and not cleaned:
        checks.append("text_became_empty")
    if original_length >= 200 and reduction is not None and reduction > 50:
        checks.append("large_character_reduction_check_manually_may_be_html")
    if isinstance(cleaned, str) and re.search(r"</?[A-Za-z][^>]*>", cleaned):
        checks.append("residual_html")
    return {
        "id": analysis["id"], "collection_date": analysis["collection_date"],
        "input_row": analysis["input_row"], "output_row": analysis["output_row"],
        "source_object_key": analysis["source_object_key"], "selected_field": field, "comparison_field": comparison_field,
        "source_characters": original_length, "clean_characters": clean_length,
        "character_reduction_pct": reduction,
        "source_paragraphs": len(re.findall(r"\S(?:.*?)(?=\n\s*\n|$)", original or "", re.S)),
        "clean_paragraphs": len(re.split(r"\n\s*\n", cleaned)) if cleaned else 0,
        "changed": original != cleaned, "checks": checks,
        "whitespace_only_change": (isinstance(original, str) and isinstance(cleaned, str)
                                   and original != cleaned and re.sub(r"\s+", " ", original).strip() == re.sub(r"\s+", " ", cleaned).strip()),
        "review_status": "pending_human_review",
        "before_excerpt": original[:2000] if isinstance(original, str) else None,
        "after_excerpt": cleaned[:2000] if isinstance(cleaned, str) else None,
        "before_truncated": original_length > 2000, "after_truncated": clean_length > 2000,
        "flags": analysis["flags"],
    }


def select_review_samples(samples: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    ordered = sorted(samples, key=lambda sample: (not bool(sample["checks"] or sample["flags"]), not sample["changed"],
                                                  sample["collection_date"], str(sample["id"]), sample["output_row"]))
    chosen = []
    seen_ids = set()
    for sample in ordered:
        if sample["id"] not in seen_ids:
            chosen.append(sample)
            seen_ids.add(sample["id"])
        if len(chosen) == limit:
            return chosen
    for sample in ordered:
        if sample not in chosen:
            chosen.append(sample)
        if len(chosen) == limit:
            break
    return chosen


def interpretations(completeness: dict[str, Any], rows: int) -> list[str]:
    notes = []
    for name, profile in completeness.items():
        if rows and profile["missing_count"] == rows:
            notes.append(f"{name}: absent en el 100% de les captures analitzades. "
                         "Cal valorar la disponibilitat a la font i la rellevancia per al projecte; "
                         "no es considera automaticament un error ni s'omple amb zero.")
        elif profile["missing_count"]:
            notes.append(f"{name}: {profile['missing_count']}/{rows} captures sense valor "
                         f"({profile['missing_pct']:.1f}%). Cal revisar-ne l'aplicabilitat abans d'imputar.")
    notes += ["Un camp complet no garanteix exactitud semantica. Cap avis nomes significa que passen les regles implementades.",
              "Els extrems numerics son indicadors descriptius, no errors confirmats; no es modifiquen les dades.",
              "Les mostres abans/despres requereixen revisio humana de significat, idiomes i paragrafs; no han estat certificades automaticament."]
    return notes


def overview_report(partitions: list[tuple[dict[str, Any], pa.Table]]) -> dict[str, Any]:
    rows = [row for _, table in partitions for row in table.to_pylist()]
    columns = sorted({name for _, table in partitions for name in table.column_names})
    numbers = sorted({name for _, table in partitions for name in numeric_columns(table)})
    json_fields = set().union(*(set(json.loads((table.schema.metadata or {}).get(b"json_encoded_fields", b"[]"))) for _, table in partitions))
    completeness = completeness_profile(rows, columns, json_fields)
    games = Counter(row["id"] for row in rows if type(row.get("id")) is int and row["id"] > 0)
    flags = Counter()
    occurrences = Counter()
    for report, _ in partitions:
        flags.update(report["after"]["flag_counts"])
        occurrences.update(report["after"]["flag_counts"].keys())
    samples = [sample for report, _ in partitions for sample in report["qualitative_review"]["samples"]]
    return {
        "scope": "all_current_trusted_partitions; capture_weighted_statistics",
        "collection_dates": sorted(report["collection_date"] for report, _ in partitions),
        "capture_count": len(rows), "distinct_valid_game_ids": len(games),
        "captures_per_game": {str(game_id): count for game_id, count in sorted(games.items())},
        "input_rows": sum(report["before"]["rows"] for report, _ in partitions),
        "warnings": sum(report["after"]["rows_with_warnings"] for report, _ in partitions),
        "exact_duplicates_removed": sum(report["corrections"]["exact_duplicates_removed"] for report, _ in partitions),
        "text_changed_rows": sum(report["corrections"]["text_changed_rows"] for report, _ in partitions),
        "recurring_flags": {name: {"row_count": flags[name], "dates_count": occurrences[name]} for name in sorted(flags)},
        "partitions": [{"date": report["collection_date"], "rows": table.num_rows,
                        "warnings": report["after"]["rows_with_warnings"]} for report, table in partitions],
        "completeness": completeness, "numeric_profile": numeric_profile(rows, numbers),
        "qualitative_review": {"status": "pending_human_review", "samples": select_review_samples(samples, limit=8)},
        "interpretation": [f"La mostra conte {len(rows)} captures de {len(games)} jocs diferents. "
                           "Les captures repetides d'un joc no son observacions independents; "
                           "els percentatges i distribucions es calculen sobre captures, no sobre jocs unics. "
                           "La mostra no representa necessariament el cataleg de RAWG.", *interpretations(completeness, len(rows))],
    }


def analysis_markdown(report: dict[str, Any], *, global_report: bool = False) -> str:
    if global_report:
        completeness, numbers = report["completeness"], report["numeric_profile"]
    else:
        completeness, numbers = report["after"]["completeness"], report["after"]["numeric_profile"]
    lines = ["", "## Interpretacio de la qualitat", ""]
    lines.extend(f"- {note}" for note in report["interpretation"])
    lines += ["", "## Perfil numeric descriptiu", "",
              "Valors finits; NaN/infinits i nuls es compten separadament al JSON. "
              "Quartils i percentils amb interpolacio lineal. Les distribucions son de les captures.", "",
              "| Camp | Finits | Nuls % | Minim | Mediana | Maxim | P95 | Extrems IQR orientatius |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, profile in numbers.items():
        lines.append(f"| {name} | {profile['finite_count']} | {profile['null_pct']:.1f} | {profile['min']} | "
                     f"{profile['median']} | {profile['max']} | {profile['p95']} | {profile['potential_extremes']['count']} |")
    lines += ["", "## Completesa", "", "| Camp amb absencies | Nuls | Text buit | Absents % |", "|---|---:|---:|---:|"]
    missing = [(name, profile) for name, profile in completeness.items() if profile["missing_count"]]
    for name, profile in missing:
        lines.append(f"| {name} | {profile['null_count']} | {profile['empty_text_count']} | {profile['missing_pct']:.1f} |")
    if not missing:
        lines.append("| Cap camp amb valors absents | 0 | 0 | 0.0 |")
    lines += ["", "## Mostra qualitativa abans/despres", "",
              "Revisio humana pendent: comparar significat, informativitat, idiomes i paragrafs. "
              "Les reduccions grans son avisos orientatius i poden ser nomes HTML eliminat. "
              "Fragments de fins a 2000 caracters; les marques de truncament no impliquen perdua al Parquet."]
    for sample in report["qualitative_review"]["samples"]:
        lines += ["", f"### Joc {sample['id']} - {sample['collection_date']} - fila {sample['output_row'] + 1}", "",
                  f"Origen: `{sample['source_object_key']}`. Camp: {sample['selected_field']}. "
                  f"Longitud: {sample['source_characters']} -> {sample['clean_characters']}. "
                  f"Canvi nomes d'espais/salts de linia: {sample['whitespace_only_change']}. "
                  f"Comprovacions: {', '.join(sample['checks']) or 'cap senyal automatic'}. "
                  f"Fragments truncats abans/despres: {sample['before_truncated']}/{sample['after_truncated']}."]
        for label, text in (("Abans", sample["before_excerpt"]), ("Despres", sample["after_excerpt"])):
            text = text if text is not None else "(sense text utilitzable)"
            fence = "`" * max(3, max((len(value) + 1 for value in re.findall(r"`+", text)), default=3))
            lines += ["", label, "", fence + "text", text, fence]
    return "\n".join(lines) + "\n"

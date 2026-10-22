"""Analitza i neteja incrementalment els Parquet de text de Formatted."""

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


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def quality_markdown(report: dict[str, Any]) -> str:
    before, after, corrections = report["before"], report["after"], report["corrections"]
    lines = [
        f"# Qualitat dels textos RAWG: {report['collection_date']}", "",
        f"Mostra analitzada: {before['rows']} captures d'aquesta data UTC. "
        "Els resultats descriuen aquesta particio; no es generalitzen a tot RAWG.", "",
        "| Indicador | Abans | Despres |", "|---|---:|---:|",
        f"| Files | {before['rows']} | {after['rows']} |",
        f"| Text absent (nul o buit) | {before['selected_text']['null'] + before['selected_text']['empty_or_whitespace']} | {after['description_clean']['null'] + after['description_clean']['empty_or_whitespace']} |",
        f"| Text amb HTML detectable | {before['selected_text']['html']} | {after['description_clean']['html']} |",
        f"| Text amb caracters de control | {before['selected_text']['control_characters']} | {after['description_clean']['control_characters']} |",
        f"| Text no buit sota el llindar orientatiu | {before['selected_text']['short_nonempty']} | {after['description_clean']['short_nonempty']} |", "",
        "## Correccions", "",
        f"- Duplicats exactes eliminats: {corrections['exact_duplicates_removed']}.",
        f"- Textos modificats a la columna neta: {corrections['text_changed_rows']}.",
        f"- Textos obtinguts de description com a alternativa: {corrections['html_fallback_rows']}.",
        "- Al Parquet es conserva description_clean com a unica columna de descripcio. "
        "Les altres columnes de negoci i les captures d'altres dies es conserven. "
        "La procedencia es consulta als manifests i als informes, no al Parquet.",
        "- Les descripcions originals continuen disponibles a Formatted i Landing; "
        "els diagnostics nomes es desen als informes.", "",
        "## Incidencies pendents", "",
        f"Files amb avisos: {after['rows_with_warnings']}. Els avisos no eliminen les files.",
    ]
    lines.extend(f"- {flag}: {count}." for flag, count in after["flag_counts"].items())
    if not after["flag_counts"]:
        lines.append("No s'han detectat incidencies amb les regles implementades.")
    if report["issues"]:
        lines += ["", "### Localitzacio de les files amb incidencies", "",
                  "Els numeros de fila seguents comencen en 1 i no inclouen cap capcalera.", ""]
        for issue in report["issues"]:
            lines.append(
                f"- Fila Trusted {issue['output_row'] + 1} (entrada {issue['input_row'] + 1}); "
                f"ID {issue['id']}; data {issue['collection_date']}; "
                f"origen `{issue['source_object_key']}`; avisos: {', '.join(issue['flags'])}."
            )
    lines += ["", "## Criteris i limits", "",
              f"El llindar de {report['rules']['min_description_chars']} caracters nomes genera un avis; no determina per si sol la validesa del text.",
              "Es comproven identificadors, dates, procedencia i la sintaxi/estructura dels camps JSON. "
              "La neteja elimina HTML i contingut script/style, ajusta espais i caracters de control. "
              "No s'imputen valors, no es tradueix, no es canvien majuscules o accents i no es generen embeddings. "
              "No s'apliquen rangs numerics no confirmats ni correccions ortografiques especulatives.", "",
              "El JSON adjunt inclou nuls per columna, estadistiques de longitud i l'analisi de cada fila "
              "amb ID, data, ruta d'origen, camp de descripcio utilitzat, longitud i avisos "
              "(indexs de fila comencant en 0). "
              "La deduplicacio i les comparacions de descripcions es fan dins d'aquesta data; "
              "un mateix joc en dates diferents conserva totes les captures.", ""]
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
            print("[SKIP] Resum global de qualitat ja actualitzat")
            return
    datasets = []
    for partition in partitions:
        checkpoint_bytes = read_bytes(client, bucket, partition["checkpoint_key"])
        if digest(checkpoint_bytes) != partition["checkpoint_sha256"]:
            raise ValueError("Una particio ha canviat durant l'analisi global")
        checkpoint = json.loads(checkpoint_bytes)
        day = partition["date"]
        report_key = f"{prefix}quality/collection_date={day}.json"
        output_key = f"{prefix}collection_date={day}/game_details.parquet"
        report_bytes = read_bytes(client, bucket, report_key)
        data = read_bytes(client, bucket, output_key)
        if digest(report_bytes) != checkpoint["outputs"][report_key] or digest(data) != checkpoint["outputs"][output_key]:
            raise ValueError("Les dades o l'informe han canviat durant l'analisi global")
        datasets.append((json.loads(report_bytes), pq.ParquetFile(pa.BufferReader(data)).read()))
    report = overview_report(datasets)
    report["quality_version"] = QUALITY_VERSION
    report["provenance"] = signature
    lines = ["# Analisi global de qualitat dels textos RAWG", "",
             f"Captures: {report['capture_count']}. Jocs diferents amb ID valid: {report['distinct_valid_game_ids']}. "
             f"Dates: {', '.join(report['collection_dates']) or 'cap'}.", "",
             f"Files amb avisos: {report['warnings']}. Duplicats exactes eliminats: {report['exact_duplicates_removed']}.", "",
             "## Dates i incidencies recurrents", "", "| Data | Captures Trusted | Files amb avisos |", "|---|---:|---:|"]
    for partition in report["partitions"]:
        lines.append(f"| {partition['date']} | {partition['rows']} | {partition['warnings']} |")
    for flag, counts in report["recurring_flags"].items():
        lines.append(f"- {flag}: {counts['row_count']} files en {counts['dates_count']} dates.")
    markdown = "\n".join(lines) + "\n" + analysis_markdown(report, global_report=True)
    outputs = {
        f"{prefix}quality/overview.json": (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"), "application/json"),
        f"{prefix}quality/overview.md": (markdown.encode("utf-8"), "text/markdown; charset=utf-8"),
    }
    for key, (body, content_type) in outputs.items():
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        if read_bytes(client, bucket, key) != body:
            raise ValueError(f"No s'ha pogut verificar {key}")
    checkpoint = {"input": signature, "outputs": {key: digest(body) for key, (body, _) in outputs.items()}}
    body = json.dumps(checkpoint, ensure_ascii=False, indent=2).encode("utf-8")
    client.put_object(Bucket=bucket, Key=checkpoint_key, Body=body, ContentType="application/json")
    if read_bytes(client, bucket, checkpoint_key) != body:
        raise ValueError("No s'ha pogut verificar el registre del resum global")
    print(f"[OK] Resum global: {report['capture_count']} captures, {report['distinct_valid_game_ids']} jocs diferents")


def trust_texts(*, dry_run: bool = False, min_description_chars: int = 50) -> int:
    if min_description_chars < 1:
        raise ValueError("El llindar de longitud ha de ser positiu")
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
                raise ValueError("El Parquet Formatted no coincideix amb el seu registre")
            signature = {"quality_version": QUALITY_VERSION, "min_description_chars": min_description_chars,
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
                    print(f"[SKIP] {day}: Trusted i informes ja actualitzats")
                    continue

            table = pq.ParquetFile(pa.BufferReader(raw)).read()
            if source_checkpoint["row_count"] != table.num_rows:
                raise ValueError("El nombre de files Formatted no coincideix amb el registre")
            cleaned, report = assess_and_clean(
                table, day, min_description_chars=min_description_chars,
                row_provenance=source_checkpoint["row_provenance"],
                expected_sources={item["key"]: item["sha256"] for item in source_checkpoint["inputs"]},
            )
            report["provenance"] = signature
            if dry_run:
                print(f"[DRY] {day}: {table.num_rows} -> {cleaned.num_rows} files, {report['after']['rows_with_warnings']} amb avisos")
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
                    raise ValueError(f"No s'ha pogut verificar {output}")
                if output == output_key:
                    restored = pq.ParquetFile(pa.BufferReader(stored)).read()
                    # Arrow equality treats NaN as unequal; preserve and flag it instead.
                    if (restored.schema != cleaned.schema or
                        json.dumps(restored.to_pylist(), sort_keys=True) != json.dumps(cleaned.to_pylist(), sort_keys=True)):
                        raise ValueError("El Parquet Trusted no coincideix amb la taula neta")
            checkpoint = {"input": signature, "outputs": {output: digest(body) for output, (body, _) in outputs.items()},
                          "rows": cleaned.num_rows, "warnings": report["after"]["rows_with_warnings"]}
            checkpoint_body = json.dumps(checkpoint, ensure_ascii=False, indent=2).encode("utf-8")
            client.put_object(Bucket=target_bucket, Key=checkpoint_key, Body=checkpoint_body, ContentType="application/json")
            if read_bytes(client, target_bucket, checkpoint_key) != checkpoint_body:
                raise ValueError("No s'ha pogut verificar el registre Trusted")
            written += 1
            partitions.append({"date": day, "checkpoint_key": checkpoint_key, "checkpoint_sha256": digest(checkpoint_body)})
            print(f"[OK] {day}: {cleaned.num_rows} files, {cleaned.num_columns} columnes, {report['after']['rows_with_warnings']} amb avisos")
        except Exception as exc:
            errors += 1
            print(f"[ERROR] {day}: {exc}", file=sys.stderr)
    if not dry_run and not errors:
        try:
            update_overview(client, target_bucket, target_prefix, partitions)
        except Exception as exc:
            errors += 1
            print(f"[ERROR] Resum global: {exc}", file=sys.stderr)
    elif errors:
        print("[WARN] El resum global no s'actualitza per evitar presentar una analisi parcial com a completa.")
    print(f"Trusted: {written} particions escrites, {skipped} omeses, {errors} errors.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Analitza sense escriure a MinIO.")
    parser.add_argument("--min-description-chars", type=int, default=50, help="Llindar orientatiu per marcar textos curts; no s'eliminen.")
    args = parser.parse_args()
    if args.min_description_chars < 1:
        parser.error("--min-description-chars ha de ser positiu")
    raise SystemExit(trust_texts(dry_run=args.dry_run, min_description_chars=args.min_description_chars))


if __name__ == "__main__":
    main()

"""Converteix captures RAWG persistents a Parquet per data de recollida UTC."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
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
from src.formatting.text_input_formats import SUPPORTED_EXTENSIONS, decode_capture


# Fixed types prevent all-null columns from changing type between daily batches.
INTEGER_FIELDS = {
    "id", "metacritic", "rating_top", "added", "playtime", "screenshots_count",
    "movies_count", "creators_count", "achievements_count", "parent_achievements_count",
    "reddit_count", "twitch_count", "youtube_count", "reviews_text_count",
    "ratings_count", "suggestions_count", "parents_count", "additions_count",
    "game_series_count", "reviews_count",
}
TEXT_FIELDS = {
    "slug", "name", "name_original", "description", "released", "updated",
    "background_image", "background_image_additional", "website", "reddit_url",
    "reddit_name", "reddit_description", "reddit_logo", "metacritic_url",
    "saturated_color", "dominant_color", "description_raw",
}
JSON_FIELDS = {
    "metacritic_platforms", "ratings", "reactions", "added_by_status",
    "alternative_names", "user_game", "parent_platforms", "platforms", "stores",
    "developers", "genres", "tags", "publishers", "esrb_rating", "clip",
}
BASE_FIELDS = INTEGER_FIELDS | TEXT_FIELDS | JSON_FIELDS | {"tba", "rating"}
FORMAT_VERSION = 2


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def json_to_table(captures: list[tuple[dict[str, Any], dict[str, str]]]) -> pa.Table:
    """Keep scalar types and encode nested/unknown values as reversible JSON text."""
    extra = set().union(*(payload.keys() for payload, _ in captures)) - BASE_FIELDS
    json_fields = JSON_FIELDS | extra
    fields = []
    for name in sorted(BASE_FIELDS | extra):
        kind = (pa.int64() if name in INTEGER_FIELDS else
                pa.bool_() if name == "tba" else
                pa.float64() if name == "rating" else pa.string())
        fields.append(pa.field(name, kind))
    schema = pa.schema(fields, metadata={
        b"format_version": str(FORMAT_VERSION).encode(),
        b"json_encoded_fields": json.dumps(sorted(json_fields)).encode(),
    })
    rows = []
    for payload, _ in captures:
        row = {}
        for field in fields:
            value = payload.get(field.name)
            if field.name in json_fields:
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None
            elif value is not None:
                if field.name in INTEGER_FIELDS and (type(value) is not int):
                    raise ValueError(f"El camp {field.name} no es un enter")
                if field.name in TEXT_FIELDS and not isinstance(value, str):
                    raise ValueError(f"El camp {field.name} no es text")
                if field.name == "tba" and type(value) is not bool:
                    raise ValueError("El camp tba no es boolea")
                if field.name == "rating" and type(value) not in (int, float):
                    raise ValueError("El camp rating no es numeric")
            row[field.name] = value
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=schema)


def format_texts(*, dry_run: bool = False) -> int:
    storage = load_storage_config()
    source_bucket = storage["buckets"]["landing"]
    target_bucket = storage["buckets"]["formatted"]
    landing = storage["landing"]
    source_prefix = f"{landing['persistent_prefix'].strip('/')}/{landing['texts_prefix'].strip('/')}/rawg/"
    target_prefix = f"{storage['formatted']['texts_prefix'].strip('/')}/rawg/"
    client = create_minio_client()
    client.head_bucket(Bucket=target_bucket)
    extensions = "|".join(SUPPORTED_EXTENSIONS)
    pattern = re.compile(r"rawg\$game_details_(\d+)\$(\d{2}-\d{2}-\d{4})\.(?:" + extensions + r")", re.IGNORECASE)
    batches: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for key in list_object_keys(client, source_bucket, source_prefix):
        match = pattern.fullmatch(key[len(source_prefix):])
        if match:
            game_id, date = match.groups()
            day = datetime.strptime(date, "%d-%m-%Y").date().isoformat()
            batches[day].append((key, int(game_id), date))

    errors = written = skipped = 0
    for day, sources in sorted(batches.items()):
        destination = f"{target_prefix}collection_date={day}/game_details.parquet"
        checkpoint_key = f"{target_prefix}manifests/collection_date={day}.json"
        try:
            captures = []
            inputs = []
            seen_ids = set()
            for key, game_id, date in sorted(sources):
                if game_id in seen_ids:
                    raise ValueError(f"Mes d'un fitxer per joc i dia: {game_id}, {day}")
                seen_ids.add(game_id)
                raw = read_bytes(client, source_bucket, key)
                digest = sha256(raw)
                payload = decode_capture(raw, key.rsplit(".", 1)[1], game_id,
                                         integer_fields=INTEGER_FIELDS, text_fields=TEXT_FIELDS)
                record_key = f"{source_prefix}manifests/rawg$text_sources_{game_id}${date}.jsonl"
                record = json.loads(read_bytes(client, source_bucket, record_key))
                collected = datetime.fromisoformat(record["ingested_at_utc"])
                if (collected.tzinfo is None or collected.astimezone(timezone.utc).date().isoformat() != day
                    or record["source_game_id"] != game_id or record["source"] != "rawg"
                    or record["bucket"] != source_bucket or record["persistent_object_key"] != key
                    or record["sha256"] != digest):
                    raise ValueError(f"Procedencia inconsistent: {key}")
                inputs.append({"key": key, "sha256": digest})
                captures.append((payload, {
                    "source": "rawg", "ingested_at_utc": record["ingested_at_utc"],
                    "source_object_key": key, "source_sha256": digest,
                }))

            row_provenance = [
                {"row_index": index, "id": payload["id"], **provenance}
                for index, (payload, provenance) in enumerate(captures)
            ]
            old_bytes = read_optional(client, target_bucket, checkpoint_key)
            old = json.loads(old_bytes) if old_bytes else None
            if old:
                previous = {entry["key"]: entry["sha256"] for entry in old["inputs"]}
                current = {entry["key"]: entry["sha256"] for entry in inputs}
                if any(current.get(key) != digest for key, digest in previous.items()):
                    raise ValueError("Un original processat ha canviat o ha desaparegut de persistent")
                if (old.get("format_version") == FORMAT_VERSION and old["inputs"] == inputs
                    and old.get("row_provenance") == row_provenance):
                    output = read_optional(client, target_bucket, destination)
                    if output is not None and sha256(output) == old["output_sha256"]:
                        print(f"[SKIP] {destination}: {len(captures)} files ja processades")
                        skipped += 1
                        continue
            table = json_to_table(captures)
            if dry_run:
                print(f"[DRY] {destination}: {table.num_rows} files, {table.num_columns} columnes")
                continue
            buffer = pa.BufferOutputStream()
            pq.write_table(table, buffer, compression="snappy")
            output = buffer.getvalue().to_pybytes()
            client.put_object(Bucket=target_bucket, Key=destination, Body=output, ContentType="application/vnd.apache.parquet")
            stored = read_bytes(client, target_bucket, destination)
            if stored != output or not pq.ParquetFile(pa.BufferReader(stored)).read().equals(table):
                raise ValueError("El Parquet pujat no coincideix amb la taula original")
            checkpoint = {
                "format_version": FORMAT_VERSION, "source_bucket": source_bucket,
                "output_key": destination, "output_sha256": sha256(output),
                "row_count": table.num_rows, "inputs": inputs,
                "row_index_base": 0,
                "row_provenance": row_provenance,
            }
            checkpoint_bytes = json.dumps(checkpoint, ensure_ascii=False, indent=2).encode("utf-8")
            client.put_object(Bucket=target_bucket, Key=checkpoint_key, Body=checkpoint_bytes, ContentType="application/json")
            if read_bytes(client, target_bucket, checkpoint_key) != checkpoint_bytes:
                raise ValueError("No s'ha pogut verificar el registre de processament")
            written += 1
            print(f"[OK] {destination}: {table.num_rows} files, {table.num_columns} columnes")
        except Exception as exc:
            errors += 1
            print(f"[ERROR] {day}: {exc}", file=sys.stderr)
    print(f"Formatted: {written} particions escrites, {skipped} omeses, {errors} errors.")
    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Valida les dades sense escriure a MinIO.")
    args = parser.parse_args()
    raise SystemExit(format_texts(dry_run=args.dry_run))


if __name__ == "__main__":
    main()

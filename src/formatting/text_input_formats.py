"""Decode one game capture per file into the common RAWG record structure.

Formatting changes representation only; HTML removal and text cleaning belong
to Trusted. No format is inferred from content: the extension is the contract.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any


SUPPORTED_EXTENSIONS = ("json", "jsonl", "ndjson", "csv", "tsv", "txt", "md", "html")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_object)


def decode_capture(body: bytes, extension: str, game_id: int, *, integer_fields: set[str],
                   text_fields: set[str]) -> dict[str, Any]:
    """Read UTF-8 (optional BOM), preserving original paragraph/line endings.

    CSV/TSV contain a header and exactly one record. Typed columns use JSON
    scalar notation, nested/unknown fields use JSON, and \\N denotes null.
    Empty text cells stay empty strings; empty non-text cells represent null.
    """
    text = body.decode("utf-8-sig")
    extension = extension.lower()
    if extension == "json":
        payload = _json(text)
    elif extension in {"jsonl", "ndjson"}:
        records = [_json(line) for line in text.splitlines() if line.strip()]
        if len(records) != 1:
            raise ValueError("Exactly one game is required per JSONL/NDJSON file")
        payload = records[0]
    elif extension in {"csv", "tsv"}:
        reader = csv.reader(io.StringIO(text, newline=""),
                            delimiter="\t" if extension == "tsv" else ",", strict=True)
        records = list(reader)
        if len(records) != 2:
            raise ValueError("A header and exactly one game are required per CSV/TSV file")
        names, values = records
        if (not names or any(not name.strip() for name in names)
            or len(set(names)) != len(names) or len(names) != len(values)):
            raise ValueError("Duplicate/empty header or inconsistent cell count")
        payload = {}
        for name, value in zip(names, values):
            if value == r"\N" or (not value and name not in text_fields):
                payload[name] = None
            elif name in text_fields:
                payload[name] = value
            else:
                # Both known structured fields and future fields remain reversible.
                payload[name] = _json(value)
                if name in integer_fields and type(payload[name]) is not int:
                    raise ValueError(f"Field {name} is not an integer")
    elif extension in {"txt", "md", "html"}:
        payload = {"id": game_id, "description" if extension == "html" else "description_raw": text}
    else:
        raise ValueError(f"Unsupported text format: {extension}")
    if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] != game_id:
        raise ValueError("An object with an integer ID matching the filename is required")
    return payload

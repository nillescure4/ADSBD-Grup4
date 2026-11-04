"""Resolve search hits to their exact, verified Trusted snapshot and row."""

import hashlib
from html import escape
import json
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.ingestion.text_temporal_to_persistent import read_bytes


def highlight_description(text: str, start: int, end: int) -> str:
    """Bold exactly the winning chunk, escaping source HTML and Markdown.

    Whitespace is preserved so paragraphs and the complete description remain
    visible. Offsets identify the occurrence even if a passage is repeated.
    """
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise ValueError("Invalid matching passage offsets.")
    return ('<div style="white-space: pre-wrap; overflow-wrap: anywhere; margin-bottom: 1.25rem;">'
            + escape(text[:start]) + '<strong>' + escape(text[start:end]) + '</strong>'
            + escape(text[end:]) + '</div>')


def load_snapshot(client: Any, bucket: str, key: str, expected_hash: str) -> list[dict[str, Any]]:
    body = read_bytes(client, bucket, key)
    if hashlib.sha256(body).hexdigest() != expected_hash:
        raise ValueError("The Trusted snapshot has changed since its embeddings were generated.")
    table = pq.ParquetFile(pa.BufferReader(body)).read()
    json_fields = json.loads((table.schema.metadata or {}).get(b"json_encoded_fields", b"[]"))
    rows = table.to_pylist()
    for row in rows:
        for field in json_fields:
            if row.get(field) is not None:
                row[field] = json.loads(row[field])
    return rows


def select_game(rows: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    """Do not substitute a newer snapshot for an older search match."""
    metadata = result["metadata"]
    index = metadata["trusted_row"]
    if type(index) is not int or not 0 <= index < len(rows):
        raise ValueError("The indexed Trusted row is unavailable.")
    row = rows[index]
    if row.get("id") != result["game_id"]:
        raise ValueError("The Trusted row does not match the retrieved game.")
    text = row.get("description_clean")
    if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != metadata["text_sha256"]:
        raise ValueError("The Trusted description does not match the indexed text.")
    if metadata.get("representation") == "full_description":
        if text != result["text"]:
            raise ValueError("The retrieved document does not match the complete Trusted description.")
    elif text[metadata["char_start"]:metadata["char_end"]] != result["text"]:
        raise ValueError("The retrieved chunk does not match the Trusted description.")
    return row


def names(value: Any, *, nested: str | None = None) -> str:
    """Display common RAWG lists without inventing absent values."""
    if not isinstance(value, list):
        return "Not available"
    labels = []
    for item in value:
        if nested and isinstance(item, dict):
            item = item.get(nested)
        label = item.get("name") if isinstance(item, dict) else item if isinstance(item, str) else None
        if isinstance(label, str) and label and label not in labels:
            labels.append(label)
    return ", ".join(labels) if labels else "Not available"

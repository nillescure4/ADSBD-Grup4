#!/usr/bin/env python3
"""Collect RAWG text sources locally and optionally upload to Landing temporal.

Accept an image collector's games.jsonl manifest or explicit RAWG game IDs.
Read credentials from .env and use config/storage.yml for MinIO destinations.
The original local-only collector remains in src/data_collection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.common.config import PROJECT_ROOT, load_storage_config
from src.common.minio_client import create_minio_client, upload_file
from src.ingestion.text_temporal_to_persistent import read_optional

RAWG_BASE = "https://api.rawg.io/api"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_session() -> requests.Session:
    retry = Retry(
        total=6,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "ADSDB-video-game-project/1.0 (academic, non-commercial)",
        "Accept": "application/json",
    })
    return session


def rawg_get(session: requests.Session, api_key: str, endpoint: str, timeout: int = 45) -> Dict[str, Any]:
    response = session.get(
        f"{RAWG_BASE}{endpoint}",
        params={"key": api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def iter_games(manifest: Path) -> Iterable[Dict[str, Any]]:
    with manifest.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[WARN] invalid JSON at line {line_no}: {exc}", file=sys.stderr)
                continue
            if row.get("source_game_id") is None:
                print(f"[WARN] line {line_no}: missing source_game_id", file=sys.stderr)
                continue
            yield row


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def manifest_rows(content: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in content.splitlines() if line.strip()]


def collection_day(timestamp: str) -> str:
    collected = datetime.fromisoformat(timestamp)
    if collected.tzinfo is None:
        raise ValueError("Collection timestamp must include a timezone")
    return collected.astimezone(timezone.utc).date().isoformat()


def parse_args() -> argparse.Namespace:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Collect raw RAWG game-detail JSON used as game descriptions."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--games-manifest",
        help="Path to games.jsonl produced by collect_rawg_images.py.",
    )
    source.add_argument("--game-ids", type=int, nargs="+", help="RAWG IDs for a small text-only test.")
    parser.add_argument("--upload-minio", action="store_true", help="Upload raw JSON and manifests to Landing temporal (summary stays local).")
    parser.add_argument(
        "--output",
        default="data/landing/text_descriptions",
        help="Output directory for raw JSON and the text-source manifest.",
    )
    parser.add_argument("--api-key", default=os.getenv("RAWG_API_KEY"))
    parser.add_argument("--max-games", type=int, default=None, help="Optional limit for testing.")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay between RAWG requests in seconds.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("Missing RAWG API key. Set RAWG_API_KEY or pass --api-key.")

    games_manifest = Path(args.games_manifest) if args.games_manifest else None
    if games_manifest is not None and not games_manifest.exists():
        raise SystemExit(f"Manifest not found: {games_manifest}")
    if args.max_games is not None and args.max_games < 1:
        raise SystemExit("--max-games must be positive.")
    if args.game_ids and any(game_id < 1 for game_id in args.game_ids):
        raise SystemExit("--game-ids must be positive.")

    client = create_minio_client() if args.upload_minio else None
    storage = load_storage_config()
    bucket = storage["buckets"]["landing"]
    prefix = f"{storage['landing']['temporal_prefix']}/{storage['landing']['texts_prefix']}"
    if client is not None:
        client.head_bucket(Bucket=bucket)

    output_root = Path(args.output)
    run_day = collection_day(utc_now())
    details_dir = output_root / "raw_api" / "rawg" / "game_details" / run_day
    manifest_path = output_root / "manifests" / "text_sources.jsonl"
    local_rows = manifest_rows(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    remote_manifest_key = f"{prefix}/manifests/text_sources.jsonl"
    remote_rows = []
    if client is not None:
        content = read_optional(client, bucket, remote_manifest_key)
        remote_rows = manifest_rows(content.decode("utf-8")) if content else []

    session = build_session()
    counters = {
        "games_seen": 0,
        "rawg_details_saved": 0,
        "rawg_details_skipped": 0,
        "manifest_rows_recovered": 0,
        "errors": 0,
        "rawg_details_uploaded": 0,
    }

    games = iter_games(games_manifest) if games_manifest else (
        {"source_game_id": game_id} for game_id in args.game_ids
    )
    for idx, game in enumerate(games, start=1):
        if args.max_games is not None and idx > args.max_games:
            break

        counters["games_seen"] += 1
        rawg_id = int(game["source_game_id"])
        game_name = game.get("name") or f"rawg_{rawg_id}"
        asset_id = f"rawg:{rawg_id}:game_details"
        detail_path = details_dir / f"{rawg_id}.json"
        print(f"[{counters['games_seen']}] {rawg_id} - {game_name}")

        try:
            row = next((record for record in reversed(local_rows)
                        if record["asset_id"] == asset_id
                        and collection_day(record["ingested_at_utc"]) == run_day), None)
            cached_path = output_root / row["raw_object_path"] if row else detail_path
            cached = row is not None and cached_path.exists()
            if cached and row.get("sha256"):
                cached = hashlib.sha256(cached_path.read_bytes()).hexdigest() == row["sha256"]
            object_key = f"{prefix}/{rawg_id}.json"
            if client is not None:
                pending = read_optional(client, bucket, object_key)
                if pending is not None:
                    remote_row = next((record for record in reversed(remote_rows)
                                       if record["asset_id"] == asset_id), None)
                    if remote_row is None or collection_day(remote_row["ingested_at_utc"]) != run_day:
                        raise ValueError("Promote the pending temporal capture before collecting a new day")
            if cached:
                detail_path = cached_path
                counters["rawg_details_skipped"] += 1
                print("  [SKIP] Today's RAWG capture already present")
            else:
                details = rawg_get(session, args.api_key, f"/games/{rawg_id}")
                collected_at = utc_now()
                if collection_day(collected_at) != run_day:
                    raise ValueError("UTC date changed during collection; restart the command")
                save_json(detail_path, details)
                counters["rawg_details_saved"] += 1
                print("  [OK] RAWG game details")
                row = {
                    "asset_id": asset_id,
                    "modality": "text",
                    "text_source_type": "game_description_source",
                    "source": "rawg",
                    "source_game_id": rawg_id,
                    "game_name": details.get("name") or game_name,
                    "raw_object_path": detail_path.relative_to(output_root).as_posix(),
                    "expected_text_fields": ["description_raw", "description"],
                    "ingested_at_utc": collected_at,
                    "sha256": hashlib.sha256(detail_path.read_bytes()).hexdigest(),
                }
                # Save provenance before uploading so retries keep the actual fetch date.
                append_jsonl(manifest_path, row)
                local_rows.append(row)
            if client is not None:
                if pending is not None and pending != detail_path.read_bytes():
                    raise ValueError("Temporal capture differs; refusing to overwrite it")
                remote_rows = [record for record in remote_rows
                               if not (record["asset_id"] == asset_id
                                       and collection_day(record["ingested_at_utc"]) == run_day)]
                remote_rows.append(row)
                client.put_object(
                    Bucket=bucket, Key=remote_manifest_key,
                    Body=("".join(json.dumps(record, ensure_ascii=False) + "\n"
                                  for record in remote_rows)).encode("utf-8"),
                    ContentType="application/x-ndjson",
                )
                upload_file(client, detail_path, bucket, object_key)
                counters["rawg_details_uploaded"] += 1
                print(f"  [OK] s3://{bucket}/{object_key}")
        except Exception as exc:
            counters["errors"] += 1
            # HTTP exception messages can contain the API key in the URL.
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            print(f"  [WARN] Game {rawg_id} failed ({message})", file=sys.stderr)

        if args.delay > 0:
            time.sleep(args.delay)

    session.close()
    summary_path = output_root / "collection_summary.json"
    save_json(summary_path, {
        "completed_at_utc": utc_now(),
        "source": "RAWG",
        "games_manifest": str(games_manifest) if games_manifest else None,
        "game_ids": args.game_ids,
        "minio_bucket": bucket if client is not None else None,
        "minio_prefix": prefix if client is not None else None,
        **counters,
    })
    if counters["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

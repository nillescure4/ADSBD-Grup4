#!/usr/bin/env python3
"""
RAWG screenshot collector for a multimodal video-game project.

What it does
------------
- Selects games from the RAWG API.
- Fetches a configurable number of screenshots per game.
- Stores the original bytes without resizing/converting them (Landing Zone behavior).
- Writes JSONL manifests with provenance and SHA-256 hashes.
- Is resumable: already downloaded source URLs are skipped.
- Supports either a single query or a stratified collection plan.

RAWG requires an API key and attribution. See:
https://rawg.io/apidocs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.rawg.io/api"
DEFAULT_PAGE_SIZE = 40

SAFE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:80] or "game"

def build_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "ADSDB-video-game-project/1.0 (academic, non-commercial)",
            "Accept": "application/json",
        }
    )
    return session

def api_get(
    session: requests.Session,
    api_key: str,
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    q = dict(params or {})
    q["key"] = api_key
    url = f"{BASE_URL}{endpoint}"
    r = session.get(url, params=q, timeout=timeout)
    r.raise_for_status()
    return r.json()

def load_existing_urls(manifest_path: Path) -> Set[str]:
    urls: Set[str] = set()
    if not manifest_path.exists():
        return urls
    with manifest_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if row.get("source_url"):
                    urls.add(row["source_url"])
            except json.JSONDecodeError:
                continue
    return urls

def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

def iter_games(
    session: requests.Session,
    api_key: str,
    filters: Dict[str, Any],
    target_games: int,
    globally_seen_game_ids: Set[int],
    delay_s: float,
) -> Iterable[Dict[str, Any]]:
    page = 1
    yielded = 0
    while yielded < target_games:
        params = {k: v for k, v in filters.items() if v not in (None, "", [])}
        params["page"] = page
        params["page_size"] = min(DEFAULT_PAGE_SIZE, target_games - yielded)
        payload = api_get(session, api_key, "/games", params)

        results = payload.get("results", [])
        if not results:
            break

        for game in results:
            game_id = game.get("id")
            if game_id is None or game_id in globally_seen_game_ids:
                continue
            globally_seen_game_ids.add(game_id)
            yielded += 1
            yield game
            if yielded >= target_games:
                break

        if not payload.get("next"):
            break
        page += 1
        if delay_s:
            time.sleep(delay_s)

def get_screenshots(
    session: requests.Session,
    api_key: str,
    game_id: int,
    limit: int,
    delay_s: float,
) -> List[Dict[str, Any]]:
    payload = api_get(
        session,
        api_key,
        f"/games/{game_id}/screenshots",
        {"page_size": max(1, min(40, limit))},
    )
    if delay_s:
        time.sleep(delay_s)
    shots = [s for s in payload.get("results", []) if not s.get("hidden", False)]
    return shots[:limit]

def suffix_from_response(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in SAFE_SUFFIXES:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in SAFE_SUFFIXES:
        return guessed
    return ".bin"

def download_bytes(
    session: requests.Session,
    url: str,
    timeout: int = 60,
) -> tuple[bytes, str]:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "")

def normalize_game_metadata(game: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": "rawg",
        "source_game_id": game.get("id"),
        "slug": game.get("slug"),
        "name": game.get("name"),
        "released": game.get("released"),
        "rating": game.get("rating"),
        "metacritic": game.get("metacritic"),
        "genres": [g.get("slug") or g.get("name") for g in game.get("genres", [])],
        "platforms": [
            p.get("platform", {}).get("slug") or p.get("platform", {}).get("name")
            for p in game.get("platforms", [])
        ],
        "rawg_url": f"https://rawg.io/games/{game.get('slug')}" if game.get("slug") else None,
    }

def collect_stratum(
    *,
    session: requests.Session,
    api_key: str,
    output_root: Path,
    stratum_name: str,
    target_games: int,
    screenshots_per_game: int,
    filters: Dict[str, Any],
    seen_game_ids: Set[int],
    seen_urls: Set[str],
    delay_s: float,
    dry_run: bool,
) -> Dict[str, int]:
    image_manifest = output_root / "manifests" / "images.jsonl"
    game_manifest = output_root / "manifests" / "games.jsonl"

    selected = 0
    with_images = 0
    downloaded = 0
    skipped_existing = 0
    errors = 0

    print(f"\n=== Stratum: {stratum_name} ===")
    print(f"Target games: {target_games}")
    print(f"Filters: {filters}")

    for game in iter_games(
        session, api_key, filters, target_games, seen_game_ids, delay_s
    ):
        selected += 1
        gm = normalize_game_metadata(game)
        gm.update({"collection_stratum": stratum_name, "selected_at_utc": utc_now()})
        append_jsonl(game_manifest, gm)

        if dry_run:
            print(f"[DRY] {game['id']} - {game.get('name')}")
            continue

        try:
            shots = get_screenshots(
                session, api_key, int(game["id"]), screenshots_per_game, delay_s
            )
        except Exception as exc:
            errors += 1
            print(f"[WARN] screenshots failed for {game.get('name')}: {exc}", file=sys.stderr)
            continue

        if not shots:
            print(f"[INFO] no screenshots: {game.get('name')}")
            continue

        with_images += 1
        game_dir = output_root / "images" / f"{game['id']}_{slugify(game.get('slug') or game.get('name') or '')}"
        game_dir.mkdir(parents=True, exist_ok=True)

        for rank, shot in enumerate(shots, start=1):
            url = shot.get("image")
            if not url:
                continue

            if url in seen_urls:
                skipped_existing += 1
                continue

            try:
                blob, content_type = download_bytes(session, url)
                digest = hashlib.sha256(blob).hexdigest()
                suffix = suffix_from_response(url, content_type)
                short_url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
                filename = f"shot_{rank:02d}_{short_url_hash}{suffix}"
                path = game_dir / filename
                path.write_bytes(blob)

                row = {
                    **gm,
                    "asset_id": f"rawg:{game['id']}:screenshot:{short_url_hash}",
                    "modality": "image",
                    "asset_type": "gameplay_screenshot",
                    "screenshot_rank": rank,
                    "source_url": url,
                    "local_path": str(path.relative_to(output_root)),
                    "content_type": content_type,
                    "bytes": len(blob),
                    "sha256": digest,
                    "raw_width": shot.get("width"),
                    "raw_height": shot.get("height"),
                    "ingested_at_utc": utc_now(),
                }
                append_jsonl(image_manifest, row)
                seen_urls.add(url)
                downloaded += 1
                print(f"[OK] {game.get('name')} -> {path.name}")
            except Exception as exc:
                errors += 1
                print(f"[WARN] download failed {url}: {exc}", file=sys.stderr)

    return {
        "selected_games": selected,
        "games_with_images": with_images,
        "downloaded_images": downloaded,
        "skipped_existing_urls": skipped_existing,
        "errors": errors,
    }

def load_plan(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Plan must be a JSON list.")
    return data

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect raw gameplay screenshots from RAWG with provenance manifests."
    )
    p.add_argument("--api-key", default=os.getenv("RAWG_API_KEY"))
    p.add_argument("--output", default="data/landing/rawg_full")
    p.add_argument("--plan", help="JSON collection plan. Overrides single-query selection.")
    p.add_argument("--max-games", type=int, default=100)
    p.add_argument("--screenshots-per-game", type=int, default=4)
    p.add_argument("--dates", help='RAWG date range, e.g. "2015-01-01,2026-12-31"')
    p.add_argument("--genres", help='RAWG genre slugs/ids, e.g. "action,indie"')
    p.add_argument("--platforms", help='RAWG platform ids, comma-separated')
    p.add_argument("--ordering", default="-added")
    p.add_argument("--metacritic", help='Optional range, e.g. "60,100"')
    p.add_argument("--delay", type=float, default=0.15, help="Delay between API calls in seconds.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()

def main() -> int:
    args = parse_args()
    if not args.api_key:
        raise SystemExit(
            "Missing RAWG API key. Set RAWG_API_KEY or pass --api-key."
        )
    if args.max_games < 1 or args.screenshots_per_game < 1:
        raise SystemExit("--max-games and --screenshots-per-game must be >= 1")

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    image_manifest = output_root / "manifests" / "images.jsonl"
    seen_urls = load_existing_urls(image_manifest)
    seen_game_ids: Set[int] = set()

    # Also avoid re-selecting games already present in the games manifest.
    games_manifest = output_root / "manifests" / "games.jsonl"
    if games_manifest.exists():
        with games_manifest.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    gid = row.get("source_game_id")
                    if isinstance(gid, int):
                        seen_game_ids.add(gid)
                except json.JSONDecodeError:
                    pass

    if args.plan:
        strata = load_plan(Path(args.plan))
    else:
        filters = {
            "dates": args.dates,
            "genres": args.genres,
            "platforms": args.platforms,
            "ordering": args.ordering,
            "metacritic": args.metacritic,
            "exclude_additions": "true",
        }
        strata = [
            {
                "name": "single_query",
                "target_games": args.max_games,
                "filters": {k: v for k, v in filters.items() if v not in (None, "")},
            }
        ]

    session = build_session()
    totals = {
        "selected_games": 0,
        "games_with_images": 0,
        "downloaded_images": 0,
        "skipped_existing_urls": 0,
        "errors": 0,
    }

    for entry in strata:
        name = entry.get("name", "unnamed")
        target_games = int(entry.get("target_games", args.max_games))
        filters = dict(entry.get("filters", {}))
        filters.setdefault("ordering", "-added")
        filters.setdefault("exclude_additions", "true")
        stats = collect_stratum(
            session=session,
            api_key=args.api_key,
            output_root=output_root,
            stratum_name=name,
            target_games=target_games,
            screenshots_per_game=int(entry.get("screenshots_per_game", args.screenshots_per_game)),
            filters=filters,
            seen_game_ids=seen_game_ids,
            seen_urls=seen_urls,
            delay_s=args.delay,
            dry_run=args.dry_run,
        )
        for k in totals:
            totals[k] += stats[k]

    summary = {
        "completed_at_utc": utc_now(),
        "source": "RAWG",
        "output_root": str(output_root),
        **totals,
    }
    (output_root / "collection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n=== Summary ===")
    print(json.dumps(summary, indent=2))
    return 1 if totals["errors"] else 0

if __name__ == "__main__":
    raise SystemExit(main())

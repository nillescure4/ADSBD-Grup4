"""Upload verified local RAWG captures without requesting or modifying source data."""

import argparse
import hashlib
import json
from pathlib import Path

from src.common.config import load_storage_config
from src.common.minio_client import create_minio_client, upload_file
from src.ingestion.text_temporal_to_persistent import read_optional


def selected_captures(root, games_manifest):
    games = {row["source_game_id"] for row in
             (json.loads(line) for line in games_manifest.read_text(encoding="utf-8").splitlines() if line.strip())}
    latest = {}
    for line in (root / "manifests/text_sources.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        game = row["source_game_id"]
        if game in games and (game not in latest or row["ingested_at_utc"] > latest[game]["ingested_at_utc"]):
            latest[game] = row
    if not games or set(latest) != games:
        raise ValueError("The local description captures do not cover the selected game manifest")
    captures = []
    for game, row in sorted(latest.items()):
        path = (root / row["raw_object_path"].replace("\\", "/")).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("A text capture path escapes its source directory")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != row["sha256"] or json.loads(body).get("id") != game:
            raise ValueError(f"Invalid local description capture for game {game}")
        captures.append((game, row, path, body))
    return captures


def upload_captures(client, storage, root, games_manifest):
    captures = selected_captures(root, games_manifest)
    landing = storage["landing"]
    bucket = storage["buckets"]["landing"]
    prefix = landing["temporal_prefix"].strip("/") + "/" + landing["texts_prefix"].strip("/")
    for game, _, _, body in captures:
        existing = read_optional(client, bucket, f"{prefix}/{game}.json")
        if existing is not None and existing != body:
            raise ValueError(f"A different Temporal capture already exists for game {game}")
    key = f"{prefix}/manifests/text_sources.jsonl"
    previous = read_optional(client, bucket, key) or b""
    selected = {game for game, *_ in captures}
    rows = [row for row in (json.loads(line) for line in previous.splitlines() if line.strip())
            if row["source_game_id"] not in selected]
    rows.extend(row for _, row, _, _ in captures)
    client.put_object(Bucket=bucket, Key=key, Body="".join(json.dumps(row) + "\n" for row in rows).encode("utf-8"),
                      ContentType="application/x-ndjson")
    for game, _, path, body in captures:
        upload_file(client, path, bucket, f"{prefix}/{game}.json")
        if read_optional(client, bucket, f"{prefix}/{game}.json") != body:
            raise ValueError(f"Could not verify the uploaded capture for game {game}")
    return len(captures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--games-manifest", type=Path, required=True)
    args = parser.parse_args()
    count = upload_captures(create_minio_client(), load_storage_config(), args.input_dir, args.games_manifest)
    print(f"Uploaded {count} verified RAWG descriptions")


if __name__ == "__main__":
    main()

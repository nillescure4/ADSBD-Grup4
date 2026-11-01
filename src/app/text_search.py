"""Read-only text retrieval independent of Streamlit.

Queries and indexed descriptions share the exact OpenCLIP configuration.
Ranking uses the best cosine similarity over query/document chunk pairs;
each game is returned once, optionally considering only its latest capture.
"""

from pathlib import Path
from typing import Any

from src.exploitation.text.build_embeddings import (
    COLLECTION, PROJECT_ROOT, configuration, split_text,
)


DB_PATH = PROJECT_ROOT / "data/exploitation/chroma"


def connect(db_path: Path = DB_PATH) -> Any:
    # PersistentClient otherwise silently creates an empty database on a bad path.
    if not (db_path / "chroma.sqlite3").is_file():
        raise ValueError("No text embedding database is available yet.")
    import chromadb
    from chromadb.errors import NotFoundError

    try:
        return chromadb.PersistentClient(path=str(db_path)).get_collection(
            COLLECTION, embedding_function=None,
        )
    except NotFoundError as exc:
        raise ValueError("The text embedding collection has not been created yet.") from exc


def inventory(collection: Any) -> dict[str, Any]:
    rows = collection.get(include=["metadatas"])
    metadata = rows["metadatas"] or []
    dates = sorted({row["collection_date"] for row in metadata})
    return {"vectors": len(rows["ids"]), "games": len({row["game_id"] for row in metadata}),
            "captures": len({row["asset_id"] for row in metadata}), "dates": dates}


def search(collection: Any, encoder: Any, query: str, *, top_k: int = 5,
           latest_only: bool = True) -> list[dict[str, Any]]:
    if not query.strip():
        raise ValueError("Enter a description to start searching.")
    if len(query) > 4000:
        raise ValueError("The query can contain at most 4,000 characters.")
    if top_k < 1:
        raise ValueError("The number of results must be positive.")
    _, fingerprint = configuration(encoder, False)
    if (collection.metadata or {}).get("config_sha256") != fingerprint:
        raise ValueError("The query model does not match the available embeddings.")
    if collection.configuration.get("hnsw", {}).get("space") != "cosine":
        raise ValueError("The collection does not use the expected distance metric.")
    metadata = collection.get(include=["metadatas"])["metadatas"] or []
    if not metadata:
        return []
    latest = {}
    for row in metadata:
        latest[row["game_id"]] = max(latest.get(row["game_id"], ""), row["collection_date"])
    chunks = split_text(query, encoder.count_tokens, encoder.budget)
    # Query chunks separately to bound model memory for long inputs.
    embeddings = [encoder.encode([chunk.text])[0] for chunk in chunks]
    # Retrieve every fragment so history filtering cannot hide a relevant game.
    # This exhaustive reranking suits the current small local corpus.
    result = collection.query(query_embeddings=embeddings, n_results=len(metadata),
                              include=["documents", "metadatas", "distances"])
    games = {}
    for documents, records, distances in zip(result["documents"], result["metadatas"], result["distances"]):
        for document, row, distance in zip(documents, records, distances):
            game_id = row["game_id"]
            if latest_only and row["collection_date"] != latest[game_id]:
                continue
            score = max(-1.0, min(1.0, 1.0 - float(distance)))
            previous = games.get(game_id)
            if previous is None or (score, row["collection_date"]) > (previous["score"], previous["date"]):
                games[game_id] = {"game_id": game_id, "score": score, "text": document,
                                  "date": row["collection_date"], "metadata": row}
    return sorted(games.values(), key=lambda row: (-row["score"], row["game_id"]))[:top_k]

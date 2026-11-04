"""Read-only audio-to-description retrieval in the pinned CLAP space."""

import math
from src.common.chroma_reads import ReliableReads

from src.app.audio_search import validate_audio_vector
from src.app.text_search import DB_PATH
from src.exploitation.audio.embedding_audio import DIMENSION, MODEL_ID, MODEL_REVISION
from src.exploitation.text.build_embeddings import sha256
from src.exploitation.text.build_clap_embeddings import COLLECTION, FINGERPRINT, validate_collection


def connect_clap_texts(path=DB_PATH):
    if not (path / "chroma.sqlite3").is_file():
        raise ValueError("Prepare the CLAP description embeddings before searching from audio.")
    import chromadb
    collection = chromadb.PersistentClient(path=str(path)).get_collection(COLLECTION, embedding_function=None)
    validate_collection(collection)
    return ReliableReads(collection)


def search_clap_descriptions(collection, vector, *, top_k=5):
    validate_collection(collection)
    vector = validate_audio_vector(vector)
    if top_k < 1:
        raise ValueError("The result limit must be positive.")
    count = collection.count()
    if not count:
        return []
    response = collection.query(query_embeddings=[vector], n_results=count, include=["documents", "metadatas", "distances"])
    if len(response["ids"][0]) != count:
        raise ValueError("Restart Streamlit after changing the CLAP description index.")
    games = {}
    for item, document, meta, distance in zip(response["ids"][0], response["documents"][0], response["metadatas"][0], response["distances"][0]):
        if (meta.get("config_sha256") != FINGERPRINT or meta.get("model_id") != MODEL_ID
                or meta.get("model_revision") != MODEL_REVISION or meta.get("dimension") != DIMENSION
                or meta.get("modality") != "text"
                or meta.get("representation") not in {"full_description", "clap_text_truncated"} or sha256(document) != meta.get("encoded_text_sha256")
                or not math.isfinite(float(distance))):
            raise ValueError("A description result has incompatible CLAP provenance.")
        hit = {"id": item, "game_id": meta["game_id"], "text": document, "date": meta["collection_date"],
               "score": max(-1., min(1., 1. - float(distance))), "metadata": meta}
        prior = games.get(hit["game_id"])
        if prior is None or hit["date"] > prior["date"] or (hit["date"] == prior["date"] and hit["score"] > prior["score"]):
            games[hit["game_id"]] = hit
    return sorted(games.values(), key=lambda hit: (-hit["score"], hit["game_id"]))[:top_k]

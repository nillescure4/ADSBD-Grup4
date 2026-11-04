"""Verified Jina image retrieval and text/image cross-modal search."""

from io import BytesIO
import hashlib
import math

from PIL import Image, ImageOps, UnidentifiedImageError
from src.common.chroma_reads import ReliableReads

from src.app.text_search import DB_PATH
from src.exploitation.images.build_embeddings import COLLECTION as IMAGE_COLLECTION, configuration as image_configuration, encode_images
from src.exploitation.text.build_embeddings import configuration as text_configuration, valid_vector
from src.exploitation.jina_clip import DIMENSION, MODEL_ID, MODEL_REVISION


def connect_images(db_path=DB_PATH):
    if not (db_path / "chroma.sqlite3").is_file():
        raise ValueError("The image embedding database is unavailable.")
    import chromadb
    from chromadb.errors import NotFoundError
    try:
        return ReliableReads(chromadb.PersistentClient(path=str(db_path)).get_collection(IMAGE_COLLECTION, embedding_function=None))
    except NotFoundError:
        raise ValueError("Prepare the Jina image embeddings before searching images.") from None


def validate_collection(collection, encoder, modality):
    settings, fingerprint = image_configuration(encoder) if modality == "image" else text_configuration(encoder, False)
    metadata = collection.metadata or {}
    if metadata.get("config_sha256") != fingerprint:
        raise ValueError(f"The {modality} collection does not match the pinned Jina model configuration.")
    if (metadata.get("model_id") != MODEL_ID or metadata.get("model_revision") != MODEL_REVISION
        or metadata.get("dimension") != DIMENSION or metadata.get("normalization") != "L2"
        or collection.configuration.get("hnsw", {}).get("space") != "cosine"):
        raise ValueError("Retrieval requires the same pinned model, 1024 dimensions, L2 normalization and cosine distance.")
    return settings


def decode_upload(body):
    if not body or len(body) > 20 * 1024 * 1024:
        raise ValueError("Choose an image file of up to 20 MB.")
    try:
        with Image.open(BytesIO(body)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("Choose an image with at most 40 million pixels.")
            return ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("The file could not be decoded as an image.") from None


def load_image(client, metadata, trusted_bucket, prefix="images/"):
    key = metadata["trusted_object_key"]
    if metadata["trusted_bucket"] != trusted_bucket or not key.startswith(prefix):
        raise ValueError("The image does not reference the configured Trusted zone.")
    body = client.get_object(Bucket=trusted_bucket, Key=key)["Body"].read()
    if hashlib.sha256(body).hexdigest() != metadata["trusted_object_sha256"]:
        raise ValueError("The Trusted image has changed since its embedding was generated.")
    return decode_upload(body)


def rank_vector(collection, vector, *, modality, top_k=5, exclude_ids=()):
    if not valid_vector(vector, DIMENSION):
        raise ValueError("The query vector is invalid or incompatible with Jina CLIP v2.")
    if top_k < 1:
        raise ValueError("The number of results must be positive.")
    listing = collection.get(include=["metadatas"])
    metadata = listing["metadatas"] or []
    if not metadata:
        return []
    latest = {}
    if modality == "text":
        for row in metadata:
            latest[row["game_id"]] = max(latest.get(row["game_id"], ""), row["collection_date"])
    response = collection.query(query_embeddings=[vector], n_results=len(metadata),
                                include=["documents", "metadatas", "distances"])
    if len(response["ids"][0]) != len(metadata):
        raise ValueError("The search index is out of date. Restart Streamlit after updating embeddings.")
    results = {}
    for item, document, row, distance in zip(response["ids"][0], response["documents"][0], response["metadatas"][0], response["distances"][0]):
        if item in exclude_ids:
            continue
        if (row.get("dimension") != DIMENSION or row.get("model_revision") != MODEL_REVISION
            or row.get("model_id") != MODEL_ID or row.get("modality") != modality
            or row.get("config_sha256") != collection.metadata["config_sha256"]):
            raise ValueError("A result has incompatible model provenance.")
        if not math.isfinite(float(distance)):
            raise ValueError("The index returned a non-finite distance.")
        result = {"id": item, "score": max(-1., min(1., 1.-float(distance))), "metadata": row}
        if modality == "text":
            game_id = row["game_id"]
            if row["collection_date"] != latest[game_id]:
                continue
            result.update(game_id=game_id, text=document, date=row["collection_date"])
            key = game_id
        else:
            key = row["asset_id"]
        if key not in results or result["score"] > results[key]["score"]:
            results[key] = result
    return sorted(results.values(), key=lambda row: (-row["score"], str(row["id"])))[:top_k]


def search_images(collection, encoder, *, image=None, text=None, top_k=5, exclude_ids=()):
    validate_collection(collection, encoder, "image")
    if (image is None) == (text is None):
        raise ValueError("Provide exactly one text or image query.")
    if image is not None:
        vector = encode_images(encoder, [image.convert("RGB")])[0]
    else:
        if not text.strip() or len(text) > 4000:
            raise ValueError("Enter a text query of between 1 and 4,000 characters.")
        # Jina's reference implementation uses retrieval.query for both
        # text-to-image and text-to-text search.
        vector = encoder.encode_query(text)
    return rank_vector(collection, vector, modality="image", top_k=top_k, exclude_ids=exclude_ids)


def search_from_image(text_collection, image_collection, encoder, image, *, targets, top_k=5, exclude_ids=()):
    for name, collection in (("text", text_collection), ("image", image_collection)):
        if name in targets:
            validate_collection(collection, encoder, name)
    if not targets or set(targets) - {"text", "image"}:
        raise ValueError("Choose text, images, or both as result modalities.")
    vector = encode_images(encoder, [image.convert("RGB")])[0]
    return {name: rank_vector(text_collection if name == "text" else image_collection, vector,
                              modality=name, top_k=top_k, exclude_ids=exclude_ids if name == "image" else ())
            for name in targets}


def search_from_text(text_collection, image_collection, encoder, text, *, targets, top_k=5):
    if not text.strip() or len(text) > 4000:
        raise ValueError("Enter a text query of between 1 and 4,000 characters.")
    if not targets or set(targets) - {"text", "image"}:
        raise ValueError("Choose text, images, or both as result modalities.")
    for name in targets:
        validate_collection(text_collection if name == "text" else image_collection, encoder, name)
    vector = encoder.encode_query(text)
    return {name: rank_vector(text_collection if name == "text" else image_collection, vector,
                              modality=name, top_k=top_k) for name in targets}

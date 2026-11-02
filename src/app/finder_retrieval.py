"""Game-level ranking fusion and verified same-game multimodal candidates."""

import base64
from io import BytesIO

from src.app.game_finder import require_candidates
from src.app.trusted_details import names
from src.app.visual_search import rank_vector, validate_collection
from src.exploitation.images.build_embeddings import encode_images


PRIORITIES = ("content", "visual", "balanced")
RRF_CONSTANT = 60


def fuse_rankings(rankings, identity):
    """Equal-weight reciprocal rank fusion; duplicates get one vote per list."""
    scores, records = {}, {}
    for ranking in rankings:
        seen = set()
        position = 0
        for hit in ranking:
            item = identity(hit)
            if item in seen:
                continue
            position += 1
            seen.add(item)
            scores[item] = scores.get(item, 0.) + 1. / (RRF_CONSTANT + position)
            if item not in records or hit["score"] > records[item]["score"]:
                records[item] = hit
    ordered = sorted(scores, key=lambda item: (-scores[item], str(item)))
    return [(records[item], scores[item]) for item in ordered]


def game_image_id(hit):
    value = hit["metadata"].get("game_id")
    return value if type(value) is int and value > 0 else None


def shortlist(text_rankings, image_rankings, priority):
    if priority not in PRIORITIES:
        raise ValueError("Choose content, visual or balanced priority.")
    text_ids = {hit["game_id"] for ranking in text_rankings for hit in ranking}
    image_ids = {game_image_id(hit) for ranking in image_rankings for hit in ranking}
    eligible = text_ids & (image_ids - {None})
    if len(eligible) < 8:
        raise ValueError(f"At least eight different games with descriptions and verified image/game associations are required; {len(eligible)} are available.")
    content = fuse_rankings([[hit for hit in ranking if hit["game_id"] in eligible] for ranking in text_rankings], lambda hit: hit["game_id"])
    visual = fuse_rankings([[hit for hit in ranking if game_image_id(hit) in eligible] for ranking in image_rankings], game_image_id)
    # Keep the most relevant screenshot of each selected game, never another game's image.
    image_by_game = {game_image_id(hit): hit for hit, score in visual}
    text_by_game = {hit["game_id"]: hit for hit, score in content}
    if priority == "content":
        ordered = [(hit["game_id"], score) for hit, score in content]
    elif priority == "visual":
        ordered = [(game_image_id(hit), score) for hit, score in visual]
    else:
        ranked = fuse_rankings([[text_by_game[hit["game_id"]] for hit, _ in content],
                               [text_by_game[game_image_id(hit)] for hit, _ in visual]], lambda hit: hit["game_id"])
        ordered = [(hit["game_id"], score) for hit, score in ranked]
    return [{"game_id": game_id, "result": text_by_game[game_id], "image_result": image_by_game[game_id],
             "retrieval_score": score, "priority": priority} for game_id, score in ordered[:8]]


def retrieve_multimodal_candidates(text_collection, image_collection, encoder, query, reference_image,
                                   priority, details_loader, image_loader):
    query = query.strip()
    references = reference_image if isinstance(reference_image, (list, tuple)) else ([] if reference_image is None else [reference_image])
    if (not query and not references) or len(query) > 4000 or len(references) > 3:
        raise ValueError("Provide text, an image, or both. Text is limited to 4,000 characters.")
    if priority not in PRIORITIES:
        raise ValueError("Choose content, visual or balanced priority.")
    validate_collection(text_collection, encoder, "text")
    validate_collection(image_collection, encoder, "image")
    vectors = []
    if query:
        vectors.append(encoder.encode_query(query))
    if references:
        vectors.extend(encode_images(encoder, [image.convert("RGB") for image in references]))
    texts, images = [], []
    for vector in vectors:
        texts.append(rank_vector(text_collection, vector, modality="text", top_k=max(1, text_collection.count())))
        images.append(rank_vector(image_collection, vector, modality="image", top_k=max(1, image_collection.count())))
    selected = shortlist(texts, images, priority)
    for game in selected:
        details = details_loader(game["result"])
        game.update(name=details.get("name") or f"Game #{game['game_id']}", description=details["description_clean"],
                    genres=names(details.get("genres")), rating=details.get("rating"), details=details,
                    screenshot=image_loader(game["image_result"]["metadata"]))
    require_candidates(selected)
    return selected


def model_image(image, label):
    """Bound request payloads while retaining actual screenshot content."""
    resized = image.convert("RGB").copy()
    resized.thumbnail((768, 768))
    output = BytesIO()
    resized.save(output, format="JPEG", quality=85)
    return {"label": label, "mime_type": "image/jpeg", "data": base64.b64encode(output.getvalue()).decode("ascii")}

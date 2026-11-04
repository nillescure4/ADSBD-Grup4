"""Opt-in real multimodal retrieval, Gemini vision and CLAP decision trees."""

from functools import lru_cache, partial
from itertools import product
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.getenv("ADSBD_RUN_MULTIMODAL_FINDER_TESTS") != "1",
                              reason="Requires real indexes, MinIO and a configured vision AI service")


def test_three_priorities_real_models_and_all_routes():
    import torch
    from src.app.finder_retrieval import model_image, retrieve_multimodal_candidates
    from src.app.game_finder import (
        current_options, final_recommendation, generate_multimodal_tree, prepare_audio_choices, question_order,
    )
    from src.app.generative_model import configuration, generate
    from src.app.audio_search import connect_audio, preview_audio, search_audio_text
    from src.app.text_search import connect
    from src.app.trusted_details import load_snapshot, select_game
    from src.app.visual_search import connect_images, load_image
    from src.common.config import load_storage_config
    from src.common.minio_client import create_minio_client
    from src.exploitation.audio.search_audio import ClapTextEncoder
    from src.exploitation.jina_clip import JinaEncoder

    torch.set_num_threads(4)
    config, error = configuration()
    assert not error
    client = create_minio_client()
    trusted = load_storage_config()["buckets"]["trusted"]
    texts, images, audios = connect(), connect_images(), connect_audio()
    jina, clap = JinaEncoder("cpu"), ClapTextEncoder()
    @lru_cache(maxsize=8)
    def snapshot(bucket, key, digest):
        return load_snapshot(client, bucket, key, digest)
    def details(hit):
        meta = hit["metadata"]
        return select_game(snapshot(meta["trusted_bucket"], meta["trusted_object_key"], meta["trusted_object_sha256"]), hit)
    def screenshot(meta):
        return load_image(client, meta, trusted)
    reference_record = images.get(where={"game_id": 28}, include=["metadatas"])["metadatas"][0]
    reference = screenshot(reference_record)
    report = {"model": config.model, "runs": []}
    for priority, query, references in [
        ("content", "An immersive adventure with a strong story and interesting choices", []),
        ("visual", "", [reference]),
        ("balanced", "A western adventure in a wide natural landscape", [reference]),
    ]:
        print(f"Validating {priority} priority", flush=True)
        candidates = retrieve_multimodal_candidates(texts, images, jina, query, references, priority, details, screenshot)
        assert len({game["game_id"] for game in candidates}) == 8
        assert all(game["game_id"] == game["image_result"]["metadata"]["game_id"] == game["details"]["id"] for game in candidates)
        attachments = [model_image(game["screenshot"], f"Candidate game ID {game['game_id']}") for game in candidates]
        attachments.extend(model_image(image, f"User reference image {index}") for index, image in enumerate(references, 1))
        tree = generate_multimodal_tree(query, candidates, priority, partial(generate, config), attachments)
        choices = prepare_audio_choices(tree, candidates,
            lambda description: search_audio_text(audios, clap, description, top_k=2),
            lambda meta: preview_audio(client, meta, trusted))
        assert all(pair["L"]["result"]["source_key"] != pair["R"]["result"]["source_key"] for pair in choices.values())
        for pair in choices.values():
            assert all(choice["preview"].startswith(b"RIFF") for choice in pair.values())
        reached = []
        for route in product("LR", repeat=3):
            path = ""
            for depth, branch in enumerate(route, 1):
                path += branch
                assert len(current_options(tree, candidates, path)) == 8 // 2**depth
            reached.append(final_recommendation(tree, candidates, path)[0]["game_id"])
        assert set(reached) == {game["game_id"] for game in candidates}
        report["runs"].append({"priority": priority, "reference_images": len(references), "query": query,
            "question_order": list(question_order(priority)), "candidate_ids": reached,
            "games": [{"game_id": game["game_id"], "name": game["name"]} for game in candidates],
            "tree": tree, "audio_pairs": {path: {branch: choice["result"]["source_key"] for branch, choice in pair.items()} for path, pair in choices.items()},
            "routes_verified": 8})
        print(f"Verified {priority}: eight routes and four distinct audio pairs", flush=True)
    Path("data/exploitation/multimodal_game_finder_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

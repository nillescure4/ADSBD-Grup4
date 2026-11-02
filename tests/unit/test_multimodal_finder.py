"""Game-level fusion, mandatory modality order and distinct final music choices."""

from itertools import product
import json

from PIL import Image
import pytest

from src.app.finder_retrieval import fuse_rankings, model_image, shortlist
from src.app.game_finder import (
    current_options, distinct_audio_pair, final_recommendation, generate_multimodal_tree,
    materialize_multimodal_tree, prepare_audio_choices, question_order,
)
from tests.unit.test_game_finder import candidates, model_tree


def multimodal_model_tree(priority="balanced"):
    order = question_order(priority)
    def convert(node, depth):
        if depth == 2:
            return {"left_game_id": node["left"]["game_id"], "left_reason": node["left"]["reason"],
                    "right_game_id": node["right"]["game_id"], "right_reason": node["right"]["reason"]}
        def ids(child):
            return [child["game_id"]] if "game_id" in child else ids(child["left"]) + ids(child["right"])
        evidence = {branch + "_evidence": [{"game_id": game_id, "fact": f"Description {game_id}" if order[depth] == "content" else f"Visible screenshot detail for game {game_id}"} for game_id in ids(node[branch])]
                    for branch in ("left", "right")}
        return {"modality": order[depth], **{key: node[key] for key in ("question", "left_label", "right_label")}, **evidence,
                "left": convert(node["left"], depth + 1), "right": convert(node["right"], depth + 1)}
    return {"tree": convert(model_tree()["tree"], 0)}


def wording_response():
    root = model_tree()["tree"]
    return {"questions": [{"path": path, **{key: node[key] for key in ("question", "left_label", "right_label")}}
                          for path, node in (("", root), ("L", root["left"]), ("R", root["right"]))]}


def hit(game_id, score=1., image=False, item=None):
    return {"game_id": game_id, "score": score, "id": item or str(game_id), "metadata": {"game_id": game_id}}


def test_content_visual_and_balanced_do_not_compare_cross_modal_cosines():
    texts = [hit(index, .1) for index in range(1, 11)]
    images = [hit(index, .99, True) for index in range(10, 0, -1)]
    assert [game["game_id"] for game in shortlist([texts], [images], "content")] == list(range(1, 9))
    assert [game["game_id"] for game in shortlist([texts], [images], "visual")] == list(range(10, 2, -1))
    balanced = shortlist([texts], [images], "balanced")
    changed_scores = [dict(row, score=-.9) for row in images]
    assert [row["game_id"] for row in balanced] == [row["game_id"] for row in shortlist([texts], [changed_scores], "balanced")]
    assert all(game["result"]["game_id"] == game["image_result"]["metadata"]["game_id"] for game in balanced)


def test_duplicate_screenshots_and_missing_provenance_never_inflate_game_count():
    texts = [hit(index) for index in range(1, 10)]
    images = [hit(1, item="first"), hit(1, item="second"), hit(None)] + [hit(index) for index in range(2, 9)]
    games = shortlist([texts], [images], "visual")
    assert len({game["game_id"] for game in games}) == 8
    assert games[0]["image_result"]["id"] == "first"
    with pytest.raises(ValueError, match="eight different"):
        shortlist([texts], [[hit(1)] * 8], "balanced")


def test_repeated_input_lists_do_not_count_duplicates_inside_a_ranking():
    result = fuse_rankings([[hit(1)] * 10 + [hit(2)], [hit(2), hit(1)]], lambda row: row["game_id"])
    assert result[0][1] == pytest.approx(result[1][1])


@pytest.mark.parametrize("priority", ["content", "visual", "balanced"])
def test_typed_question_order_and_all_eight_routes(priority):
    games = candidates()
    tree = materialize_multimodal_tree(multimodal_model_tree(priority), games, priority)
    leaves = []
    for route in product("LR", repeat=3):
        path = ""
        for depth, branch in enumerate(route, 1):
            path += branch
            assert len(current_options(tree, games, path)) == 8 // 2**depth
        leaves.append(final_recommendation(tree, games, path)[0]["game_id"])
    assert sorted(leaves) == list(range(1, 9))
    malformed = multimodal_model_tree(priority)
    malformed["tree"]["modality"] = "audio"
    with pytest.raises(ValueError, match="order"):
        materialize_multimodal_tree(malformed, games, priority)


def test_ai_receives_actual_labelled_images_without_internal_provenance():
    images = [model_image(Image.new("RGB", (20, 20)), f"Candidate game ID {index}") for index in range(1, 9)]
    calls = []
    def generator(system, prompt, schema, **kwargs):
        calls.append(kwargs)
        assert "private" not in prompt
        if "approved" in schema["properties"]:
            return {"approved": True, "issues": []}
        assert json.loads(prompt)["priority"] == "balanced"
        return multimodal_model_tree()
    generate_multimodal_tree("", candidates(), "balanced", generator, images)
    assert len(calls) == 2 and all(len(call["images"]) == 8 for call in calls)
    with pytest.raises(ValueError, match="screenshot"):
        generate_multimodal_tree("story", candidates(), "balanced", generator, images[:7])


def audio(item, score):
    return {"vector_id": item, "source_key": "audio/" + item, "score": score, "metadata": {"item": item}}


def test_distinct_audio_pair_optimizes_both_candidates_instead_of_only_second():
    first = [audio("same", .9), audio("alternative-a", .89)]
    second = [audio("same", .95), audio("alternative-b", .1)]
    left, right = distinct_audio_pair(first, second)
    assert left["vector_id"] == "alternative-a" and right["vector_id"] == "same"
    with pytest.raises(ValueError, match="two different"):
        distinct_audio_pair(first[:1], second[:1])


def test_all_audio_pairs_are_prepared_once_and_previews_are_reused():
    tree = materialize_multimodal_tree(multimodal_model_tree(), candidates(), "balanced")
    calls, previews = [], []
    def ranker(text):
        calls.append(text)
        return [audio("a", .8), audio("b", .7)]
    def preview(meta):
        previews.append(meta)
        return b"wav"
    choices = prepare_audio_choices(tree, candidates(), ranker, preview)
    assert set(choices) == {"LL", "LR", "RL", "RR"} and len(calls) == 8 and len(previews) == 2
    assert all(pair["L"]["result"]["source_key"] != pair["R"]["result"]["source_key"] for pair in choices.values())


@pytest.mark.parametrize("text,image_count", [("Adventure", 0), ("", 1), ("Adventure", 1), ("", 3)])
def test_retrieval_accepts_text_images_or_both_and_verifies_same_game_sources(monkeypatch, text, image_count):
    from src.app import finder_retrieval as service
    class Collection:
        def count(self):
            return 8
    class Encoder:
        def encode_query(self, query):
            assert query == text
            return ["text query"]
    calls = []
    def rank(collection, vector, *, modality, top_k):
        calls.append((vector, modality))
        return [hit(index) for index in range(1, 9)]
    monkeypatch.setattr(service, "validate_collection", lambda *args: None)
    monkeypatch.setattr(service, "rank_vector", rank)
    monkeypatch.setattr(service, "encode_images", lambda encoder, images: [[f"image {index}"] for index in range(len(images))])
    image = Image.new("RGB", (20, 20))
    result = service.retrieve_multimodal_candidates(Collection(), Collection(), Encoder(), text,
        [image] * image_count, "balanced",
        lambda row: {"id": row["game_id"], "name": "Game", "description_clean": "Verified description"},
        lambda meta: image)
    assert len(result) == 8 and len(calls) == 2 * (bool(text) + image_count)
    assert all(game["description"] == "Verified description" and game["screenshot"] is image for game in result)


@pytest.mark.parametrize("problem", ["missing_evidence", "wrong_game", "invented_quote"])
def test_unsubstantiated_content_groups_are_rejected(problem):
    response = multimodal_model_tree()
    evidence = response["tree"]["left_evidence"]
    if problem == "missing_evidence":
        evidence.pop()
    elif problem == "wrong_game":
        evidence[0]["game_id"] = 8
    else:
        evidence[0]["fact"] = "An invented feature absent from the description"
    with pytest.raises(ValueError):
        materialize_multimodal_tree(response, candidates(), "balanced")


def test_invalid_tree_repair_is_bounded_and_keeps_the_same_candidate_images():
    images = [model_image(Image.new("RGB", (10, 10)), f"Candidate game ID {index}") for index in range(1, 9)]
    calls = []
    def generator(system, prompt, schema, **kwargs):
        calls.append(prompt)
        return {"tree": {}}
    with pytest.raises(ValueError):
        generate_multimodal_tree("Adventure", candidates(), "balanced", generator, images)
    assert len(calls) == 2 and "validation_feedback" in calls[1]


def test_semantic_review_repairs_unsupported_labels_before_exposing_a_tree():
    images = [model_image(Image.new("RGB", (10, 10)), f"Candidate game ID {index}") for index in range(1, 9)]
    calls, reviews = [], []
    def generator(system, prompt, schema, **kwargs):
        calls.append(prompt)
        if "questions" in schema["properties"]:
            return wording_response()
        if "approved" in schema["properties"]:
            reviews.append(True)
            return {"approved": len(reviews) > 1, "issues": [] if len(reviews) > 1 else ["Game 1 does not support the branch claim"]}
        return multimodal_model_tree()
    tree = generate_multimodal_tree("Adventure", candidates(), "balanced", generator, images)
    assert len(calls) == 4 and len(tree["nodes"]) == 7
    assert "Game 1 does not support" in calls[2] and "previous_tree" in calls[2]


def test_semantic_review_failures_are_bounded_without_a_fabricated_fallback():
    images = [model_image(Image.new("RGB", (10, 10)), f"Candidate game ID {index}") for index in range(1, 9)]
    calls = []
    def generator(system, prompt, schema, **kwargs):
        calls.append(True)
        if "questions" in schema["properties"]:
            return wording_response()
        return {"approved": False, "issues": ["Unsupported claim"]} if "approved" in schema["properties"] else multimodal_model_tree()
    with pytest.raises(ValueError, match="grounding review"):
        generate_multimodal_tree("Adventure", candidates(), "balanced", generator, images)
    assert len(calls) == 4


def test_content_sentence_references_cannot_be_borrowed_from_another_game():
    response = multimodal_model_tree()
    for branch in ("left_evidence", "right_evidence"):
        for item in response["tree"][branch]:
            item["fact"] = f"game_{item['game_id']}_sentence_0"
    materialize_multimodal_tree(response, candidates(), "balanced")
    response["tree"]["left_evidence"][0]["fact"] = "game_8_sentence_0"
    with pytest.raises(ValueError, match="invalid game IDs"):
        materialize_multimodal_tree(response, candidates(), "balanced")


def test_wording_repair_preserves_all_game_assignments_and_evidence():
    from src.app.game_finder import apply_wording_repair
    response = multimodal_model_tree()
    repair = wording_response()
    repair["questions"][0]["question"] = "A broader, grounded contrast?"
    corrected = apply_wording_repair(response, repair)
    assert corrected["tree"]["left"] == response["tree"]["left"]
    assert corrected["tree"]["left_evidence"] == response["tree"]["left_evidence"]
    assert corrected["tree"]["question"] != response["tree"]["question"]
    repair["questions"][0]["left_ids"] = [1]
    with pytest.raises(ValueError):
        apply_wording_repair(response, repair)


@pytest.mark.parametrize("priority", ["content", "visual", "balanced"])
def test_catalogue_questions_cover_all_eight_games_without_inventing_preferences(priority):
    from src.app.game_finder import catalogue_tree
    games = candidates()
    tree = catalogue_tree(games, priority)
    reached = [final_recommendation(tree, games, "".join(path))[0]["game_id"] for path in product("LR", repeat=3)]
    assert sorted(reached) == list(range(1, 9))
    assert ("screenshots" if priority == "visual" else "descriptions") in next(node for node in tree["nodes"] if not node["path"])["question"]


def test_initial_service_failure_is_retried_automatically():
    images = [model_image(Image.new("RGB", (10, 10)), f"Candidate game ID {index}") for index in range(1, 9)]
    calls = []
    def generator(system, prompt, schema, **kwargs):
        calls.append(schema)
        if len(calls) == 1:
            raise ValueError("Temporary service outage")
        return {"approved": True, "issues": []} if "approved" in schema["properties"] else multimodal_model_tree()
    tree = generate_multimodal_tree("Adventure", candidates(), "balanced", generator, images)
    assert len(tree["nodes"]) == 7 and len(calls) == 3


def test_content_evidence_rejection_retains_a_structurally_valid_question():
    from src.app.game_finder import QuestionPreparationError
    images = [model_image(Image.new("RGB", (10, 10)), f"Candidate game ID {index}") for index in range(1, 9)]
    response = multimodal_model_tree()
    response["tree"]["left_evidence"][0]["fact"] = "An unsupported content claim"
    with pytest.raises(QuestionPreparationError) as failure:
        generate_multimodal_tree("Adventure", candidates(), "balanced", lambda *args, **kwargs: response, images)
    assert failure.value.tree is not None
    assert len(failure.value.tree["nodes"]) == 7

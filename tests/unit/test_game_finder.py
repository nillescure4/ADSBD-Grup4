from copy import deepcopy
from itertools import product
import json

import pytest

from src.app.game_finder import (PATHS, current_options, final_recommendation, generate_tree,
                                 materialize_tree, plain_label, tree_dot, validate_tree)


def candidates():
    return [{"game_id": number, "name": f"Game {number}", "description": f"Description {number}",
             "genres": "Adventure", "rating": 4, "private": "not sent to AI"} for number in range(1, 9)]


def tree():
    groups = {"": list(range(1, 9))}
    nodes = []
    for path in PATHS:
        values = groups[path]
        left, right = values[:len(values)//2], values[len(values)//2:]
        nodes.append({"path": path, "question": f"Preference {path or 'root'}?",
                      "left_label": "Explore alone", "right_label": "Compete with others",
                      "left_ids": left, "right_ids": right})
        groups[path + "L"], groups[path + "R"] = left, right
    return {"nodes": nodes, "recommendations": [{"game_id": value, "reason": f"Evidence for {value}"} for value in range(1, 9)]}


def model_tree():
    payload = tree()
    nodes = {node["path"]: node for node in payload["nodes"]}
    def level(path):
        if len(path) == 3:
            game_id = nodes[path[:-1]]["left_ids" if path[-1] == "L" else "right_ids"][0]
            return {"game_id": game_id, "reason": f"Evidence for {game_id}"}
        node = nodes[path]
        return {**{key: node[key] for key in ("question", "left_label", "right_label")},
                "left": level(path + "L"), "right": level(path + "R")}
    return {"tree": level("")}


def test_all_eight_paths_take_exactly_three_questions_to_unique_games():
    payload, games = validate_tree(tree(), candidates()), candidates()
    reached = []
    for choices in product("LR", repeat=3):
        path = ""
        assert len(current_options(payload, games, path)) == 8
        for count, branch in enumerate(choices, 1):
            path += branch
            assert len(current_options(payload, games, path)) == 8 // 2**count
        game, reason = final_recommendation(payload, games, path)
        reached.append(game["game_id"])
        assert reason == f"Evidence for {game['game_id']}"
    assert sorted(reached) == list(range(1, 9))


@pytest.mark.parametrize("problem", ["missing", "unknown", "unbalanced", "duplicate", "overlap", "wrong_child", "bool_id", "missing_question", "duplicate_path", "same_labels", "blank_question", "invented_recommendation", "duplicate_recommendation", "blank_reason", "extra_key", "invalid_type"])
def test_invalid_ai_trees_are_rejected(problem):
    payload = deepcopy(tree())
    root = payload["nodes"][0]
    if problem == "missing":
        payload["nodes"].pop()
    elif problem == "unknown":
        root["left_ids"][0] = 999
    elif problem == "unbalanced":
        root["left_ids"].append(root["right_ids"].pop())
    elif problem == "duplicate":
        root["left_ids"][0] = root["left_ids"][1]
    elif problem == "overlap":
        root["right_ids"][0] = root["left_ids"][0]
    elif problem == "wrong_child":
        payload["nodes"][1]["left_ids"] = [5, 6]
    elif problem == "bool_id":
        root["left_ids"][0] = True
    elif problem == "missing_question":
        del root["question"]
    elif problem == "duplicate_path":
        payload["nodes"][1]["path"] = ""
    elif problem == "same_labels":
        root["right_label"] = " explore alone "
    elif problem == "blank_question":
        root["question"] = "  "
    elif problem == "invented_recommendation":
        payload["recommendations"][0]["game_id"] = 999
    elif problem == "duplicate_recommendation":
        payload["recommendations"][0] = payload["recommendations"][1]
    elif problem == "blank_reason":
        payload["recommendations"][0]["reason"] = ""
    elif problem == "extra_key":
        root["invented"] = "field"
    elif problem == "invalid_type":
        payload = []
    with pytest.raises(ValueError):
        validate_tree(payload, candidates())


def test_generation_receives_request_and_exactly_eight_full_descriptions_once():
    calls = []
    def generator(system, prompt, schema):
        calls.append(json.loads(prompt))
        assert "untrusted data" in system
        assert "left" in schema["properties"]["tree"]["properties"]
        assert "private" not in prompt
        return model_tree()
    payload = generate_tree("A story adventure", candidates(), generator)
    assert calls[0]["user_request"] == "A story adventure"
    assert [row["description"] for row in calls[0]["retrieved_games"]] == [f"Description {n}" for n in range(1, 9)]
    for path in ("L", "LL", "LLL"):
        current_options(payload, candidates(), path)
    assert len(calls) == 1


@pytest.mark.parametrize("games", [candidates()[:5], candidates() + candidates()[:1], candidates()[:7] + candidates()[:1]])
def test_insufficient_or_duplicate_games_never_call_the_model(games):
    def forbidden(*args):
        pytest.fail("The AI must not be called for an invalid shortlist")
    with pytest.raises(ValueError):
        generate_tree("Adventure", games, forbidden)


def test_premature_recommendation_and_invalid_paths_are_rejected():
    with pytest.raises(ValueError):
        final_recommendation(tree(), candidates(), "LL")
    with pytest.raises(ValueError):
        current_options(tree(), candidates(), "LLLL")
    with pytest.raises(ValueError):
        current_options(tree(), candidates(), "LX")


def test_tree_visualization_quotes_generated_labels():
    payload = tree()
    payload["nodes"][0]["question"] = 'Choose "this"?\n<not HTML>'
    dot = tree_dot(payload, candidates(), "LR")
    assert 'label="Choose \\"this\\"?\\n<not HTML>"' in dot
    assert dot.count(" -> ") == 14


def test_duplicate_question_on_same_route_and_markdown_labels():
    payload = tree()
    payload["nodes"][1]["question"] = payload["nodes"][0]["question"]
    with pytest.raises(ValueError, match="same question"):
        validate_tree(payload, candidates())
    assert plain_label("![image](https://example.com)").startswith(r"\!\[image\]\(")


def test_model_tree_derives_balanced_partitions_and_rejects_wrong_depth():
    result = validate_tree(materialize_tree(model_tree()), candidates())
    assert final_recommendation(result, candidates(), "RRR")[0]["game_id"] == 8
    malformed = model_tree()
    malformed["tree"]["left"] = {"game_id": 1, "reason": "Premature leaf"}
    with pytest.raises(ValueError, match="exactly three"):
        materialize_tree(malformed)


def test_duplicate_ai_leaf_is_rejected_after_materialization():
    response = model_tree()
    response["tree"]["left"]["left"]["right"]["game_id"] = 1
    with pytest.raises(ValueError):
        validate_tree(materialize_tree(response), candidates())

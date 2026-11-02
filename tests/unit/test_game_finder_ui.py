from itertools import product
import json
import threading

from PIL import Image
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from src.app.views import generation
from src.app.generative_model import ModelConfig
from tests.unit.test_game_finder import candidates
from tests.unit.test_multimodal_finder import multimodal_model_tree


def page():
    return AppTest.from_string("from src.app.views.generation import render\nrender()", default_timeout=15)


def click(app, label):
    return next(button for button in app.button if button.label == label).click().run()


@pytest.fixture
def enabled(monkeypatch):
    calls = []
    games = candidates()
    for game in games:
        game["screenshot"] = Image.new("RGB", (20, 20), "green")
        game["retrieval_score"] = .01
        game["details"] = {"id": game["game_id"], "name": game["name"], "description_clean": game["description"], "rating": 4}
        game["result"] = {"game_id": game["game_id"], "text": game["description"], "score": 0.8,
                          "date": "2026-11-01", "metadata": {"representation": "full_description"}}
    monkeypatch.setattr(generation, "configuration", lambda: (ModelConfig("ollama", "test"), None))
    monkeypatch.setattr(generation, "dataset_status", lambda: ({"games": 8}, None))
    monkeypatch.setattr(generation, "image_status", lambda: (8, None))
    monkeypatch.setattr(generation, "audio_status", lambda: (16, None))
    monkeypatch.setattr(generation, "retrieve_candidates", lambda *args: games)
    monkeypatch.setattr(generation, "audio_model", lambda: (object(), threading.Lock()))
    monkeypatch.setattr(generation, "audio_collection", lambda: object())
    monkeypatch.setattr(generation, "search_audio_text", lambda *args, **kwargs: [
        {"vector_id": str(index), "source_key": f"audio/{index}.flac", "score": .8 - index * .1,
         "metadata": {"start_seconds": 0., "end_seconds": 10.}} for index in range(2)])
    monkeypatch.setattr(generation, "audio_preview", lambda meta: b"RIFFtest")
    def generate(*args, **kwargs):
        calls.append(args)
        if "approved" in args[3]["properties"]:
            return {"approved": True, "issues": []}
        return multimodal_model_tree(json.loads(args[2])["priority"])
    monkeypatch.setattr(generation, "generate", generate)
    return calls


def test_missing_model_and_five_game_catalogue_disable_generation(monkeypatch):
    monkeypatch.setattr(generation, "configuration", lambda: (None, "Configure the AI model"))
    monkeypatch.setattr(generation, "dataset_status", lambda: ({"games": 5}, None))
    monkeypatch.setattr(generation, "image_status", lambda: (0, "Image catalogue unavailable"))
    monkeypatch.setattr(generation, "audio_status", lambda: (0, "Audio catalogue unavailable"))
    app = page().run()
    assert not app.exception
    assert app.title[0].value == "Find your next game"
    assert app.button[0].disabled
    assert any("5 different games" in item.value for item in app.info)


@pytest.mark.parametrize("path,game_id", [("".join(path), index) for index, path in enumerate(product("LR", repeat=3), 1)])
@pytest.mark.parametrize("priority", ["Content", "Visual", "Balanced · no priority"])
def test_three_answers_reach_each_leaf_without_additional_ai_calls(enabled, path, game_id, priority):
    app = page().run()
    app.text_area[0].set_value("An adventure with interesting choices")
    app.radio[0].set_value(priority)
    click(app, "Find my game")
    assert not app.exception
    assert f"Question 1 of 3 · {'Visual' if priority == 'Visual' else 'Content'}" in [item.value for item in app.subheader]
    for depth, branch in enumerate(path, 1):
        label = ("Choose audio A" if branch == "L" else "Choose audio B") if depth == 3 else ("Explore alone" if branch == "L" else "Compete with others")
        click(app, label)
        assert not app.exception
        assert app.session_state[generation.STATE_KEY]["path"] == path[:depth]
    assert len(enabled) == 2
    assert any(item.value == f"01 · Game {game_id}" for item in app.subheader)
    assert any(item.value == f"Description {game_id}" for item in app.text)
    assert any(item.value == f"Evidence for {game_id}" for item in app.text)
    assert not any(item.label in {"Explore alone", "Compete with others"} for item in app.button)
    click(app, "Previous question")
    assert app.session_state[generation.STATE_KEY]["path"] == path[:2]
    assert len(enabled) == 2 and not app.exception
    click(app, "Start a new search")
    assert generation.STATE_KEY not in app.session_state
    assert len(enabled) == 2 and not app.exception


def test_model_failure_continues_to_real_catalogue_questions_without_another_click(enabled, monkeypatch):
    app = page().run()
    app.text_area[0].set_value("Story adventure")
    click(app, "Find my game")
    assert generation.STATE_KEY in app.session_state
    failed_calls = []
    def invalid_response(*args, **kwargs):
        failed_calls.append(True)
        return {"nodes": []}
    monkeypatch.setattr(generation, "generate", invalid_response)
    click(app, "Find my game")
    assert not app.exception and not app.error
    assert app.session_state[generation.STATE_KEY]["question_source"] == "catalogue"
    assert app.session_state[generation.STATE_KEY]["path"] == ""
    assert any(item.value == "Description 1" for item in app.text)
    click(app, "Choose group A")
    click(app, "Choose group A")
    click(app, "Choose audio A")
    assert app.success and not app.error and not app.exception
    assert len(failed_calls) == 1
    assert any(item.value == "01 · Game 1" for item in app.subheader)


def test_default_request_is_prefilled_and_can_be_cleared(enabled):
    app = page().run()
    assert app.text_area[0].value == "A chill game to play with my friends, not too difficult."
    app.text_area[0].set_value("")
    click(app, "Find my game")
    assert app.warning and not enabled

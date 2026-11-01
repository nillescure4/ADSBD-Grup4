from itertools import product

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from src.app.views import generation
from src.app.generative_model import ModelConfig
from tests.unit.test_game_finder import candidates, tree


def page():
    return AppTest.from_string("from src.app.views.generation import render\nrender()", default_timeout=15)


def click(app, label):
    return next(button for button in app.button if button.label == label).click().run()


@pytest.fixture
def enabled(monkeypatch):
    calls = []
    games = candidates()
    for game in games:
        game["details"] = {"id": game["game_id"], "name": game["name"], "description_clean": game["description"], "rating": 4}
        game["result"] = {"game_id": game["game_id"], "text": game["description"], "score": 0.8,
                          "date": "2026-11-01", "metadata": {"representation": "full_description"}}
    monkeypatch.setattr(generation, "configuration", lambda: (ModelConfig("ollama", "test"), None))
    monkeypatch.setattr(generation, "dataset_status", lambda: ({"games": 8}, None))
    monkeypatch.setattr(generation, "retrieve_candidates", lambda query: games)
    def generate(*args):
        calls.append(args)
        return tree()
    monkeypatch.setattr(generation, "generate", generate)
    return calls


def test_missing_model_and_five_game_catalogue_disable_generation(monkeypatch):
    monkeypatch.setattr(generation, "configuration", lambda: (None, "Configure the AI model"))
    monkeypatch.setattr(generation, "dataset_status", lambda: ({"games": 5}, None))
    app = page().run()
    assert not app.exception
    assert app.title[0].value == "Find your next game"
    assert app.button[0].disabled
    assert any("5 different games" in item.value for item in app.info)


@pytest.mark.parametrize("path,game_id", [("".join(path), index) for index, path in enumerate(product("LR", repeat=3), 1)])
def test_three_answers_reach_each_leaf_with_one_ai_call(enabled, path, game_id):
    app = page().run()
    app.text_area[0].set_value("An adventure with interesting choices")
    click(app, "Find my game")
    assert not app.exception
    assert "Question 1 of 3" in [item.value for item in app.subheader]
    for depth, branch in enumerate(path, 1):
        click(app, "Explore alone" if branch == "L" else "Compete with others")
        assert not app.exception
        assert app.session_state[generation.STATE_KEY]["path"] == path[:depth]
    assert len(enabled) == 1
    assert any(item.value == f"01 · Game {game_id}" for item in app.subheader)
    assert any(item.value == f"Description {game_id}" for item in app.text)
    assert any(item.value == f"Evidence for {game_id}" for item in app.text)
    assert not any(item.label in {"Explore alone", "Compete with others"} for item in app.button)
    click(app, "Previous question")
    assert app.session_state[generation.STATE_KEY]["path"] == path[:2]
    assert len(enabled) == 1 and not app.exception
    click(app, "Start a new search")
    assert generation.STATE_KEY not in app.session_state
    assert len(enabled) == 1 and not app.exception


def test_model_failure_displays_error_and_clears_previous_run(enabled, monkeypatch):
    app = page().run()
    app.text_area[0].set_value("Story adventure")
    click(app, "Find my game")
    assert generation.STATE_KEY in app.session_state
    monkeypatch.setattr(generation, "generate", lambda *args: {"nodes": []})
    click(app, "Find my game")
    assert not app.exception and app.error
    assert generation.STATE_KEY not in app.session_state
    assert not app.success

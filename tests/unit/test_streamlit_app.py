import threading
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from src.app import ui
from src.app.views import similarity


@pytest.fixture
def status(monkeypatch):
    info = {"games": 5, "captures": 10, "vectors": 46, "dates": ["2026-10-20", "2026-10-22"]}
    monkeypatch.setattr(ui, "dataset_status", lambda: (info, None))
    monkeypatch.setattr(similarity, "dataset_status", lambda: (info, None))


def page(module):
    return AppTest.from_string(f"from src.app.views.{module} import render\nrender()")


def test_home_and_navigation(status):
    app = AppTest.from_file(Path(__file__).resolve().parents[2] / "streamlit_app.py", default_timeout=15).run()
    assert not app.exception
    assert len(app.metric) == 3
    assert app.metric[0].value == "5"
    assert app.title[0].value == "One game. Many ways to discover it."
    assert app.metric[0].label == "Games with indexed text"


@pytest.mark.parametrize("module,title", [
    ("multimodal", "One idea, different perspectives"),
    ("generation", "From discovery to answers"),
])
def test_pending_tasks_cannot_execute(module, title):
    app = page(module).run()
    assert not app.exception
    assert app.info and all(button.disabled for button in app.button)
    assert app.title[0].value == title
    assert "Coming soon" in app.info[0].value


def test_text_search_form_and_rendering(status, monkeypatch):
    monkeypatch.setattr(similarity, "model", lambda: (object(), threading.Lock()))
    monkeypatch.setattr(similarity, "collection", lambda: object())
    monkeypatch.setattr(similarity, "game_names", lambda: {1: "Portal"})
    monkeypatch.setattr(similarity, "search", lambda *args, **kwargs: [
        {"game_id": 1, "score": 0.8, "date": "2026-10-22", "text": "<script>Untrusted text</script>",
         "metadata": {"trusted_bucket": "trusted", "trusted_object_key": "game.parquet",
                      "trusted_row": 0, "chunk_index": 0, "chunk_count": 1, "char_start": 0, "char_end": 15}}
    ])
    app = page("similarity").run()
    assert app.button[0].label == "Find games"
    app.button[0].click().run()
    assert app.warning
    app.text_area[0].input("puzzle game")
    app.button[0].click().run()
    assert not app.exception
    assert app.metric[0].value == "0.800"
    assert any("Portal" in item.value for item in app.subheader)
    assert any("<script>" in item.value for item in app.text)
    app.radio[0].set_value("Image").run()
    assert not app.exception and all(button.disabled for button in app.button)


def test_missing_data_displays_information(monkeypatch):
    monkeypatch.setattr(similarity, "dataset_status", lambda: (None, "Catalogue unavailable"))
    app = page("similarity").run()
    assert not app.exception and app.info
    assert not app.text_area

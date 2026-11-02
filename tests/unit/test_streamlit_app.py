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
    from src.app import audio_ui
    monkeypatch.setattr(audio_ui, "audio_status", lambda: (0, "Audio catalogue unavailable"))


def page(module):
    return AppTest.from_string(f"from src.app.views.{module} import render\nrender()")


def test_home_and_navigation(status):
    app = AppTest.from_file(Path(__file__).resolve().parents[2] / "streamlit_app.py", default_timeout=15).run()
    assert not app.exception
    assert len(app.metric) == 3
    assert app.metric[0].value == "5"
    assert app.title[0].value == "One game. Many ways to discover it."
    assert app.metric[0].label == "Games with indexed text"


def test_missing_audio_catalogue_displays_setup_information(monkeypatch):
    from src.app.views import multimodal
    monkeypatch.setattr(multimodal, "dataset_status", lambda: ({"games": 100}, None))
    monkeypatch.setattr(multimodal, "image_status", lambda: (50, None))
    monkeypatch.setattr(multimodal, "audio_status", lambda: (0, "Audio catalogue unavailable"))
    monkeypatch.setattr(multimodal, "clap_text_status", lambda: (0, "CLAP descriptions unavailable"))
    app = page("multimodal").run()
    app.radio[0].set_value("Audio").run()
    assert not app.exception
    assert all(button.disabled for button in app.button)
    assert app.title[0].value == "One idea, different perspectives"


def test_text_search_form_and_rendering(status, monkeypatch):
    monkeypatch.setattr(similarity, "model", lambda: (object(), threading.Lock()))
    monkeypatch.setattr(similarity, "collection", lambda: object())
    monkeypatch.setattr(similarity, "game_details", lambda result: {
        "id": 1, "name": "Portal", "rating": 0, "metacritic": None, "playtime": 0,
        "genres": [{"name": "Puzzle"}],
        "description_clean": "Full first paragraph.\n\n<script>Untrusted text</script>\n\nFull second paragraph.",
    })
    monkeypatch.setattr(similarity, "search", lambda *args, **kwargs: [
        {"game_id": 1, "score": 0.8, "date": "2026-10-22", "text": "<script>Untrusted text</script>",
         "metadata": {"trusted_bucket": "trusted", "trusted_object_key": "game.parquet",
                      "trusted_row": 0, "chunk_index": 0, "chunk_count": 1,
                      "char_start": len("Full first paragraph.\n\n"),
                      "char_end": len("Full first paragraph.\n\n<script>Untrusted text</script>")}}
    ])
    app = page("similarity").run()
    assert app.button[0].label == "Find games"
    assert "complete game descriptions" in app.info[0].value
    app.button[0].click().run()
    assert app.warning
    app.text_area[0].input("puzzle game")
    app.button[0].click().run()
    assert not app.exception
    assert app.metric[0].value == "0.800"
    assert any("Portal" in item.value for item in app.subheader)
    description = next(item.value for item in app.markdown if "Full first paragraph." in item.value)
    assert '<strong>&lt;script&gt;Untrusted text&lt;/script&gt;</strong>' in description
    assert "Full second paragraph." in description and "<script>" not in description
    assert not app.json
    assert not any(item.label in {"Source provenance", "Best-matching passage"}
                   or item.label.startswith("All Trusted fields") for item in app.expander)
    assert any(item.label == "RAWG rating" and item.value == "0 / 5" for item in app.metric)
    assert any(item.label == "Metacritic" and item.value == "Not available" for item in app.metric)
    assert any("Puzzle" in item.value for item in app.markdown)
    app.radio[0].set_value("Audio").run()
    assert not app.exception and all(button.disabled for button in app.button)


def test_missing_data_displays_information(monkeypatch):
    monkeypatch.setattr(similarity, "dataset_status", lambda: (None, "Catalogue unavailable"))
    app = page("similarity").run()
    assert not app.exception and app.info
    assert not app.text_area


def test_details_unavailable_preserves_search_and_labels_partial_text(monkeypatch):
    def unavailable(result):
        raise ValueError("Snapshot changed")
    monkeypatch.setattr(similarity, "game_details", unavailable)
    app = AppTest.from_string("""
from src.app.views.similarity import render_result
render_result(1, {'game_id': 1, 'score': 0.5, 'date': '2026-10-22', 'text': 'Indexed passage',
                 'metadata': {'trusted_bucket': 'trusted', 'trusted_object_key': 'old.parquet',
                              'trusted_row': 0, 'chunk_index': 0, 'chunk_count': 2,
                              'char_start': 0, 'char_end': 15}})
""").run()
    assert not app.exception
    assert "Only the indexed description" in app.warning[0].value
    assert not any(item.value == "Full description" for item in app.subheader)
    assert any(item.value == "Indexed passage" for item in app.text)

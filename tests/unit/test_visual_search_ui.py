import threading

import pytest
from PIL import Image

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest
import streamlit as st

from src.app import image_ui
from src.app.views import similarity, multimodal


def page(name):
    return AppTest.from_string(f"from src.app.views.{name} import render\nrender()", default_timeout=15)


def click(app, label):
    return next(button for button in app.button if button.label == label).click().run()


@pytest.fixture
def available(monkeypatch):
    image = Image.new("RGB", (20, 20), "green")
    for module in (similarity, multimodal):
        monkeypatch.setattr(module, "image_status", lambda: (50, None))
        monkeypatch.setattr(module, "dataset_status", lambda: ({"games": 100}, None))
        monkeypatch.setattr(module, "model", lambda: (object(), threading.Lock()))
        monkeypatch.setattr(module, "query_image", lambda key: (image, ("reference",)))
        monkeypatch.setattr(module, "collection", lambda: "text collection")
        monkeypatch.setattr(module, "image_collection", lambda: "image collection")
    return image


def test_task_one_image_search_is_enabled_and_displays_real_result_shape(available, monkeypatch):
    hit = {"id": "image", "score": .8, "metadata": {"game_name": "Test game", "image_id": "source screenshot"}}
    calls = []
    def search(*args, **kwargs):
        calls.append(kwargs)
        return [hit]
    monkeypatch.setattr(similarity, "search_images", search)
    monkeypatch.setattr(image_ui, "preview", lambda metadata: available)
    app = page("similarity").run()
    app.radio[0].set_value("Image").run()
    click(app, "Find similar images")
    assert not app.exception and len(calls) == 1
    assert calls[0]["exclude_ids"] == ("reference",)
    assert any(item.value == "01 · Test game" for item in app.subheader)
    assert app.metric[0].value == "0.800"


@pytest.mark.parametrize("source", ["Text", "Image"])
def test_task_two_routes_both_query_types_and_renders_both_modalities(available, monkeypatch, source):
    calls = []
    def search(*args, **kwargs):
        calls.append((args, kwargs))
        return {"text": [{"result": "description"}], "image": [{"result": "screenshot"}]}
    monkeypatch.setattr(multimodal, "search_from_text", search)
    monkeypatch.setattr(multimodal, "search_from_image", search)
    monkeypatch.setattr(multimodal, "render_result", lambda rank, hit, **kwargs: st.text("Verified description result"))
    monkeypatch.setattr(multimodal, "render_image", lambda rank, hit, **kwargs: st.text("Verified image result"))
    app = page("multimodal").run()
    if source == "Image":
        app.radio[0].set_value("Image").run()
    else:
        app.text_area[0].set_value("A western adventure").run()
    click(app, "Explore connections")
    assert not app.exception and len(calls) == 1
    assert calls[0][1]["targets"] == ["text", "image"]
    assert {item.value for item in app.text} >= {"Verified description result", "Verified image result"}
    if source == "Image":
        assert calls[0][1]["exclude_ids"] == ("reference",)


def test_missing_image_index_displays_setup_information(monkeypatch):
    monkeypatch.setattr(similarity, "dataset_status", lambda: ({"games": 100}, None))
    monkeypatch.setattr(similarity, "image_status", lambda: (0, "Prepare the Jina image embeddings"))
    app = page("similarity").run()
    app.radio[0].set_value("Image").run()
    assert not app.exception
    assert any("Prepare the Jina image embeddings" in item.value for item in app.info)


def test_empty_reference_does_not_run_the_model(available, monkeypatch):
    monkeypatch.setattr(similarity, "query_image", lambda key: (None, ()))
    monkeypatch.setattr(similarity, "search_images", lambda *args, **kwargs: pytest.fail("No search without an image"))
    app = page("similarity").run()
    app.radio[0].set_value("Image").run()
    click(app, "Find similar images")
    assert not app.exception and app.warning

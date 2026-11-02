"""App routing and source/model guards for CLAP retrieval."""

from types import SimpleNamespace
import threading

import numpy as np
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src.app import audio_search as service, audio_ui
from src.app.views import multimodal


def unit_vector():
    return [1.] + [0.] * 511


@pytest.mark.parametrize("vector", [[1., 0.], [0.] * 512, [float("nan")] * 512, [2.] + [0.] * 511])
def test_incompatible_vectors_are_rejected(vector):
    with pytest.raises(ValueError, match="512-dimensional"):
        service.validate_audio_vector(vector)


def test_source_guard_rejects_wrong_zone_and_changed_revision():
    meta = {"source_bucket": "trusted", "source_key": "audio/a.flac", "source_signature": "stale"}
    client = SimpleNamespace(head_object=lambda **kwargs: {"ETag": '"new"', "ContentLength": 10})
    with pytest.raises(ValueError, match="changed"):
        service.checked_source(client, meta, "trusted")
    with pytest.raises(ValueError, match="configured Trusted"):
        service.checked_source(client, meta, "another bucket")


def test_bad_upload_is_rejected_before_loading_tools_or_encoder():
    with pytest.raises(ValueError, match="50 MB"):
        service.uploaded_query(b"", ".mp3", None)
    with pytest.raises(ValueError, match="FLAC"):
        service.uploaded_query(b"body", ".zip", None)


def test_mismatched_query_model_revision_is_rejected():
    encoder = SimpleNamespace(model_revision="old")
    with pytest.raises(ValueError, match="pinned revision"):
        service.search_audio_text(None, encoder, "Quiet piano music")


def test_task_one_catalogue_audio_avoids_model_loading_and_excludes_reference(monkeypatch):
    monkeypatch.setattr(audio_ui, "audio_status", lambda: (16, None))
    monkeypatch.setattr(audio_ui, "query_audio", lambda key: {"vector": unit_vector(), "source_key": "audio/reference.flac"})
    monkeypatch.setattr(audio_ui, "audio_collection", lambda: "audio collection")
    monkeypatch.setattr(audio_ui, "audio_model", lambda: pytest.fail("Indexed queries reuse their vector"))
    calls = []
    def search(*args, **kwargs):
        calls.append(kwargs)
        return [{"score": .8}]
    monkeypatch.setattr(audio_ui, "search_audio", search)
    monkeypatch.setattr(audio_ui, "render_audio", lambda rank, hit: st.text("Playable audio result"))
    app = AppTest.from_string("from src.app.views.similarity import render\nrender()", default_timeout=15).run()
    app.radio[0].set_value("Audio").run()
    next(button for button in app.button if button.label == "Find similar audio").click().run()
    assert not app.exception
    assert calls[0]["source_key"] == "audio/reference.flac"
    assert any(item.value == "Playable audio result" for item in app.text)


@pytest.mark.parametrize("audio_only", [True, False])
def test_task_two_audio_only_and_mixed_results(monkeypatch, audio_only):
    monkeypatch.setattr(multimodal, "dataset_status", lambda: ({"games": 100}, None))
    monkeypatch.setattr(multimodal, "image_status", lambda: (50, None))
    monkeypatch.setattr(multimodal, "audio_status", lambda: (16, None))
    monkeypatch.setattr(multimodal, "audio_collection", lambda: "audio collection")
    monkeypatch.setattr(multimodal, "collection", lambda: "text collection")
    monkeypatch.setattr(multimodal, "image_collection", lambda: "image collection")
    monkeypatch.setattr(multimodal, "audio_model", lambda: (object(), threading.Lock()))
    monkeypatch.setattr(multimodal, "model", lambda: pytest.fail("Audio-only search must not load Jina") if audio_only else (object(), threading.Lock()))
    calls = []
    def search(*args, **kwargs):
        calls.append(args)
        return [{"score": .4}]
    monkeypatch.setattr(multimodal, "search_audio_text", search)
    monkeypatch.setattr(multimodal, "search_from_text", lambda *args, **kwargs: {"text": [{}], "image": [{}]})
    monkeypatch.setattr(multimodal, "render_audio", lambda *args, **kwargs: st.text("Audio result"))
    monkeypatch.setattr(multimodal, "render_result", lambda *args, **kwargs: st.text("Description result"))
    monkeypatch.setattr(multimodal, "render_image", lambda *args, **kwargs: st.text("Image result"))
    app = AppTest.from_string("from src.app.views.multimodal import render\nrender()", default_timeout=15).run()
    app.text_area[0].set_value("Peaceful fantasy music").run()
    if audio_only:
        app.multiselect[0].set_value(["Audio"]).run()
    next(button for button in app.button if button.label == "Explore connections").click().run()
    assert not app.exception and calls[0][2] == "Peaceful fantasy music"
    assert any(item.value == "Audio result" for item in app.text)
    if not audio_only:
        assert {item.value for item in app.text} >= {"Description result", "Image result"}

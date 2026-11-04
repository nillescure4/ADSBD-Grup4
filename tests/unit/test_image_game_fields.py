import pytest
from PIL import Image

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from src.app import image_ui


@pytest.mark.parametrize("compact", [False, True])
def test_images_show_all_verified_game_fields_in_both_layouts(monkeypatch, compact):
    class Collection:
        def get(self, **kwargs):
            assert kwargs["where"] == {"game_id": 28}
            return {"ids": ["old", "new"], "documents": ["Old description", "Current description"],
                    "metadatas": [{"collection_date": "2026-10-20"}, {"collection_date": "2026-11-01"}]}
    monkeypatch.setattr(image_ui, "collection", lambda: Collection())
    monkeypatch.setattr(image_ui, "preview", lambda metadata: Image.new("RGB", (10, 10)))
    def details(hit):
        assert hit["text"] == "Current description"
        return {"rating": 0, "metacritic": None, "playtime": 0, "released": "2018-10-26",
                "genres": [{"name": "Action"}], "platforms": [{"platform": {"name": "PC"}}],
                "developers": [{"name": "Rockstar Games"}], "publishers": [{"name": "Rockstar Games"}]}
    monkeypatch.setattr(image_ui, "game_details", details)
    app = AppTest.from_string(f"""
from src.app.image_ui import render_image
render_image(1, {{'score': .8, 'metadata': {{'game_id': 28, 'game_name': 'Red Dead Redemption 2'}}}}, compact={compact})
""").run()
    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    for expected in ("2018-10-26", "Action", "PC", "Developers", "Publishers", "Rockstar Games"):
        assert expected in rendered
    if compact:
        assert not app.metric
        for expected in ("**RAWG rating:** 0 / 5", "**Metacritic:** Not available", "**Playtime:** 0 h"):
            assert expected in rendered
    else:
        metrics = {item.label: item.value for item in app.metric}
        assert metrics["RAWG rating"] == "0 / 5"
        assert metrics["Metacritic"] == "Not available"
        assert metrics["Playtime"] == "0 h"
    assert any("RAWG ID 28" in item.value and "2026-11-01" in item.value for item in app.caption)


def test_unverified_game_details_are_not_displayed(monkeypatch):
    monkeypatch.setattr(image_ui, "preview", lambda metadata: Image.new("RGB", (10, 10)))
    def unavailable():
        raise ValueError("Trusted unavailable")
    monkeypatch.setattr(image_ui, "collection", unavailable)
    app = AppTest.from_string("""
from src.app.image_ui import render_image
render_image(1, {'score': .8, 'metadata': {'game_id': 28}}, compact=True)
""").run()
    assert not app.exception and not app.metric
    assert any("Related game details are unavailable" in item.value for item in app.caption)

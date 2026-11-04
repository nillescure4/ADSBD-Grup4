import json

from src.data_collection import collect_opengameart_audio as audio
from src.data_collection import collect_rawg_images as images


def test_rawg_collection_errors_reach_the_caller(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["collect", "--api-key", "test", "--output", str(tmp_path), "--max-games", "1"])
    monkeypatch.setattr(images, "build_session", object)
    monkeypatch.setattr(images, "collect_stratum", lambda **kwargs: {
        "selected_games": 1, "games_with_images": 0, "downloaded_images": 0,
        "skipped_existing_urls": 0, "errors": 1,
    })
    assert images.main() == 1
    assert json.loads((tmp_path / "collection_summary.json").read_text())["errors"] == 1


def test_audio_collection_errors_reach_the_caller(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["collect", "--output", str(tmp_path), "--max-audio-files", "1"])
    monkeypatch.setattr(audio, "build_session", lambda user_agent: object())
    monkeypatch.setattr(audio, "check_robots", lambda *args: (True, "Test fixture"))
    class Requester:
        def __init__(self, **kwargs):
            pass
        def get(self, *args, **kwargs):
            raise RuntimeError("Source unavailable")
    monkeypatch.setattr(audio, "PoliteRequester", Requester)
    assert audio.main() == 1
    summary = json.loads((tmp_path / "collection_summary.json").read_text())
    assert summary["errors"] == 1 and summary["direct_audio_files_total"] == 0

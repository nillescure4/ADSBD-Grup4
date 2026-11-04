from pathlib import Path

from src.common.config import PROJECT_ROOT, load_datasets_config
from src.data_collection import collect_rawg_images
from src.ingestion.audio import audio_landing


def test_collector_and_ingestion_defaults_match_catalogue(monkeypatch):
    monkeypatch.setattr("sys.argv", ["collect_rawg_images"])
    datasets = load_datasets_config()["datasets"]
    assert Path(collect_rawg_images.parse_args().output) == Path("data/landing") / datasets["rawg_images"]["directory"]
    assert audio_landing.DEFAULT_AUDIO_DIR == PROJECT_ROOT / "data/landing" / datasets["opengameart_audio"]["directory"]
    assert {entry["modality"] for entry in datasets.values()} == {"text", "image", "audio"}

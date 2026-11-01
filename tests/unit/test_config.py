from src.common.config import load_storage_config


def test_all_pipeline_zones_are_configured() -> None:
    buckets = load_storage_config()["buckets"]

    assert set(buckets) == {"landing", "formatted", "trusted", "exploitation"}
    assert len(set(buckets.values())) == 4


def test_storage_override_is_scoped_to_the_process(tmp_path, monkeypatch):
    custom = tmp_path / "storage.yml"
    custom.write_text("buckets:\n  landing: isolated-landing\n", encoding="utf-8")
    monkeypatch.setenv("ADSBD_STORAGE_CONFIG", str(custom))
    assert load_storage_config()["buckets"]["landing"] == "isolated-landing"
    monkeypatch.delenv("ADSBD_STORAGE_CONFIG")
    assert load_storage_config()["buckets"]["landing"] == "1.landing-zone"


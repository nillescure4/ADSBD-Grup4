from src.common.config import load_storage_config


def test_all_pipeline_zones_are_configured() -> None:
    buckets = load_storage_config()["buckets"]

    assert set(buckets) == {"landing", "formatted", "trusted", "exploitation"}
    assert len(set(buckets.values())) == 4


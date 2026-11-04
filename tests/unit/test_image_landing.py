"""Unit tests for image Temporal Landing ingestion."""

from pathlib import Path

import pytest

# Adjust this import if your module has another name.
from src.ingestion.images import images_to_landing as landing

STORAGE = {"buckets": {"landing": "landing"}, "landing": {"temporal_prefix": "temporal"}}
DATASET = {"modality": "image", "directory": "rawg_images", "source": "rawg"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temporary project root; the module uploads through an in-memory client."""
    monkeypatch.setattr(landing, "PROJECT_ROOT", tmp_path)

    def fake_upload(client, path, bucket, key):
        client.put(bucket, key, Path(path).read_bytes())

    monkeypatch.setattr(landing, "upload_file", fake_upload)
    return tmp_path


def write_image(project, relative, data=b"fake image bytes"):
    """Create a fake image under data/landing/rawg_images without needing a real decoder."""
    path = project / "data" / "landing" / "rawg_images" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_discovery_is_recursive_sorted_and_ignores_non_images(tmp_path):
    for relative in ("b/shot2.PNG", "a/shot1.jpg", "a/data.jsonl", "notes.txt", ".hidden.jpg"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    files = landing.iter_image_files(tmp_path)
    assert [file.relative_to(tmp_path).as_posix() for file in files] == ["a/shot1.jpg", "b/shot2.PNG"]


@pytest.mark.parametrize("name", ["a.JPG", "a.Jpeg", "a.webp", "a.BMP", "a.tif", "a.gif"])
def test_extensions_are_matched_case_insensitively(tmp_path, name):
    (tmp_path / name).write_bytes(b"x")
    assert [file.name for file in landing.iter_image_files(tmp_path)] == [name]


def test_upload_key_is_flat_under_temporal_images_dataset(project, s3):
    write_image(project, "100/a.jpg", b"first")
    write_image(project, "200/b.png", b"second")
    errors = landing.load_images_to_landing("rawg", DATASET, s3, STORAGE)
    assert errors == 0
    assert s3.keys("landing") == ["temporal/images/rawg/a.jpg", "temporal/images/rawg/b.png"]
    assert s3.data("landing", "temporal/images/rawg/a.jpg") == b"first"


def test_missing_configuration_keys_raise(project, s3):
    with pytest.raises(ValueError, match="missing keys"):
        landing.load_images_to_landing("rawg", {"modality": "image", "directory": "x"}, s3, STORAGE)


def test_non_image_dataset_is_rejected(project, s3):
    with pytest.raises(ValueError, match="not an image dataset"):
        landing.load_images_to_landing("rawg", {**DATASET, "modality": "text"}, s3, STORAGE)


def test_missing_directory_raises(project, s3):
    with pytest.raises(FileNotFoundError):
        landing.load_images_to_landing("rawg", DATASET, s3, STORAGE)


def test_empty_directory_uploads_nothing(project, s3):
    (project / "data" / "landing" / "rawg_images").mkdir(parents=True)
    assert landing.load_images_to_landing("rawg", DATASET, s3, STORAGE) == 0
    assert s3.keys("landing") == []


def test_duplicate_filename_in_two_subfolders_is_an_error_and_never_overwrites(project, s3):
    write_image(project, "100/shot.jpg", b"from 100")
    write_image(project, "200/shot.jpg", b"from 200")
    errors = landing.load_images_to_landing("rawg", DATASET, s3, STORAGE)
    assert errors == 1
    assert s3.keys("landing") == ["temporal/images/rawg/shot.jpg"]
    assert s3.data("landing", "temporal/images/rawg/shot.jpg") == b"from 100"


def test_one_failed_upload_does_not_stop_the_others(project, s3, monkeypatch):
    write_image(project, "a.jpg")
    write_image(project, "bad.jpg")
    write_image(project, "c.jpg")

    def flaky_upload(client, path, bucket, key):
        if Path(path).name == "bad.jpg":
            raise ConnectionError("network down")
        client.put(bucket, key, Path(path).read_bytes())

    monkeypatch.setattr(landing, "upload_file", flaky_upload)
    errors = landing.load_images_to_landing("rawg", DATASET, s3, STORAGE)
    assert errors == 1
    assert s3.keys("landing") == ["temporal/images/rawg/a.jpg", "temporal/images/rawg/c.jpg"]


def test_rerun_overwrites_with_the_same_content(project, s3):
    write_image(project, "a.jpg", b"same")
    landing.load_images_to_landing("rawg", DATASET, s3, STORAGE)
    assert landing.load_images_to_landing("rawg", DATASET, s3, STORAGE) == 0
    assert s3.keys("landing") == ["temporal/images/rawg/a.jpg"]
    assert s3.data("landing", "temporal/images/rawg/a.jpg") == b"same"


def configure_main(monkeypatch, s3, datasets):
    monkeypatch.setattr(landing, "load_yaml", lambda name: {"datasets": datasets})
    monkeypatch.setattr(landing, "load_storage_config", lambda: STORAGE)
    monkeypatch.setattr(landing, "create_minio_client", lambda: s3)


def test_main_without_image_datasets_exits_with_a_message(monkeypatch, s3):
    configure_main(monkeypatch, s3, {"descriptions": {"modality": "text", "directory": "t", "source": "rawg"}})
    with pytest.raises(SystemExit, match="No image dataset"):
        landing.main([])


def test_main_uploads_every_image_dataset(project, monkeypatch, s3):
    write_image(project, "a.jpg")
    configure_main(monkeypatch, s3, {"rawg": DATASET})
    landing.main([])
    assert s3.keys("landing") == ["temporal/images/rawg/a.jpg"]
    assert s3.calls["head_bucket"] == 1


def test_main_exits_1_when_a_dataset_fails_but_still_processes_the_others(project, monkeypatch, s3):
    write_image(project, "a.jpg")
    broken = {"modality": "image", "directory": "does_not_exist", "source": "other"}
    configure_main(monkeypatch, s3, {"broken": broken, "rawg": DATASET})
    with pytest.raises(SystemExit) as raised:
        landing.main([])
    assert raised.value.code == 1
    assert s3.keys("landing") == ["temporal/images/rawg/a.jpg"]

def test_explicit_collection_directory_does_not_change_shared_configuration(project, monkeypatch, s3):
    write_image(project, "shot.jpg")
    configure_main(monkeypatch, s3, {"rawg": DATASET})
    landing.main(["--dataset", "rawg", "--input-dir", str(project / "data/landing/rawg_images")])
    assert s3.keys("landing") == ["temporal/images/rawg/shot.jpg"]


def test_input_override_requires_an_explicit_dataset(monkeypatch):
    with pytest.raises(SystemExit) as failure:
        landing.main(["--input-dir", "."])
    assert failure.value.code == 2

"""Unit tests for image -> image search: the query is any image file, or a stored image."""

import sys

import chromadb
import numpy as np
import pytest
from chromadb import EmbeddingFunction
from PIL import Image

# Adjust these imports if your modules live elsewhere.
from src.exploitation import generate_image_embeddings as image_index
from src.exploitation import image_to_image as similar

RED = "images/rawg$games$1$18-10-2026.jpg"
GREEN = "images/rawg$games$2$18-10-2026.jpg"
BLUE = "images/rawg$games$3$18-10-2026.jpg"
TRUSTED = "trusted"
COLORS = {RED: (250, 10, 10), GREEN: (10, 250, 10), BLUE: (10, 10, 250)}


class FakeClip(EmbeddingFunction):
    """The mean colour of the image: similar-looking images get similar vectors, no download needed."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, input):
        self.calls.append(tuple(np.asarray(array).shape for array in input))
        return [np.asarray(array, dtype=np.float32).reshape(-1, 3).mean(axis=0) for array in input]

    @staticmethod
    def name() -> str:
        return "fake-clip"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config):
        return FakeClip()


def write_image(path, color=(200, 30, 30), mode="RGB", size=(40, 30), fmt="PNG", **save_kwargs):
    Image.new(mode, size, color).save(path, format=fmt, **save_kwargs)
    return path


@pytest.fixture
def env(monkeypatch, tmp_path):
    model = FakeClip()
    monkeypatch.setattr(image_index, "load_embedding_function", lambda device: model)
    db_path = tmp_path / "chroma"
    db = chromadb.PersistentClient(path=str(db_path))
    images = db.create_collection("images", embedding_function=model,
                                  metadata={"hnsw:space": "cosine", "model": image_index.MODEL_ID})
    keys = list(COLORS)
    images.add(ids=keys,
               embeddings=[model([np.full((8, 8, 3), COLORS[key], dtype=np.uint8)])[0] for key in keys],
               metadatas=[image_index.metadata_from_key(key) for key in keys])
    model.calls.clear()
    return type("Env", (), {"db": db, "db_path": db_path, "model": model, "images": images, "dir": tmp_path})


def search(env, **options):
    options.setdefault("n_results", 5)
    return similar.similar_images(db_path=env.db_path, device="cpu", **options)


# ---- a query image that is not part of the pipeline -----------------------------------------------------


def test_a_new_image_finds_the_stored_image_that_looks_most_like_it(env):
    query = write_image(env.dir / "mine.png", color=(200, 30, 30))
    outcome = search(env, image_file=query)
    assert outcome["results"][0]["id"] == RED
    assert outcome["query"] == str(query)


def test_every_stored_image_is_ranked_and_the_order_follows_the_distance(env):
    results = search(env, image_file=write_image(env.dir / "mine.png", color=(30, 30, 200)))["results"]
    assert [hit["id"] for hit in results][0] == BLUE
    assert [hit["distance"] for hit in results] == sorted(hit["distance"] for hit in results)
    assert results[0]["metadata"]["image_id"] == "3" and results[0]["metadata"]["dataset"] == "games"


def test_the_query_image_goes_through_the_model_once_as_an_rgb_array(env):
    search(env, image_file=write_image(env.dir / "mine.png", size=(40, 30)))
    assert env.model.calls == [((30, 40, 3),)]


def test_a_file_identical_to_a_stored_image_finds_it_at_distance_zero(env):
    """The query is preprocessed exactly like the pipeline's images, so the same picture matches itself."""
    path = write_image(env.dir / "same.png", color=(10, 250, 10))
    pixels = np.array(Image.open(path).convert("RGB"))
    env.images.upsert(ids=["images/rawg$games$9$18-10-2026.jpg"], embeddings=[env.model([pixels])[0]])
    top = search(env, image_file=path)["results"][0]
    assert top["distance"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("mode, color, kwargs", [
    ("RGBA", (200, 30, 30, 255), {}),
    ("LA", (128, 255), {}),
    ("L", 90, {}),
])
def test_other_colour_modes_are_accepted(env, mode, color, kwargs):
    query = write_image(env.dir / "mode.png", color=color, mode=mode, **kwargs)
    assert len(search(env, image_file=query)["results"]) == 3


def test_transparent_pixels_are_flattened_onto_white(tmp_path):
    path = write_image(tmp_path / "t.png", color=(0, 0, 0, 0), mode="RGBA")
    assert similar.load_query_image(path)[5, 5].tolist() == [255, 255, 255]


def test_the_exif_orientation_is_applied(env):
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees
    path = write_image(env.dir / "rotated.jpg", size=(40, 20), fmt="JPEG", exif=exif)
    assert similar.load_query_image(path).shape == (40, 20, 3)


def test_an_animated_image_is_read_from_its_first_frame(env):
    red, blue = Image.new("RGB", (16, 16), (255, 0, 0)), Image.new("RGB", (16, 16), (0, 0, 255))
    path = env.dir / "anim.gif"
    red.save(path, save_all=True, append_images=[blue], duration=100)
    pixel = similar.load_query_image(path)[5, 5]
    assert pixel[0] > 200 and pixel[2] < 60


# ---- a stored image as the query ----------------------------------------------------------------------------


def test_a_stored_image_uses_its_vector_and_never_loads_the_model(env, monkeypatch):
    def no_model(device):
        raise AssertionError("searching with a stored image must not load any model")

    monkeypatch.setattr(image_index, "load_embedding_function", no_model)
    assert search(env, image_key=RED)["query"] == RED


def test_a_stored_image_does_not_find_itself(env):
    ids = [hit["id"] for hit in search(env, image_key=RED)["results"]]
    assert RED not in ids and len(ids) == 2


def test_the_number_of_results_is_respected_and_capped(env):
    assert len(search(env, image_key=RED, n_results=1)["results"]) == 1
    assert len(search(env, image_key=RED, n_results=50)["results"]) == 2
    assert len(search(env, image_file=write_image(env.dir / "m.png"), n_results=50)["results"]) == 3


def test_an_empty_collection_returns_no_results(env):
    env.images.delete(ids=env.images.get()["ids"])
    assert search(env, image_file=write_image(env.dir / "m.png"))["results"] == []


def test_a_search_never_modifies_the_database(env):
    search(env, image_file=write_image(env.dir / "m.png"))
    search(env, image_key=GREEN)
    assert env.db.get_collection("images", embedding_function=None).count() == 3


# ---- wrong input ------------------------------------------------------------------------------------------------


def test_exactly_one_kind_of_query_is_required(env):
    path = write_image(env.dir / "m.png")
    with pytest.raises(ValueError, match="exactly one"):
        search(env)
    with pytest.raises(ValueError, match="exactly one"):
        search(env, image_file=path, image_key=RED)


def test_a_missing_file_is_a_clear_error(env):
    with pytest.raises(ValueError, match="not found"):
        search(env, image_file=env.dir / "nope.png")


def test_a_file_that_is_not_an_image_is_a_clear_error(env):
    text = env.dir / "notes.png"
    text.write_text("this is not an image")
    with pytest.raises(ValueError, match="not a readable image"):
        search(env, image_file=text)


def test_an_unknown_key_is_a_clear_error(env):
    with pytest.raises(ValueError, match="has no embedding"):
        search(env, image_key="images/unknown.jpg")


def test_the_number_of_results_must_be_positive(env):
    with pytest.raises(ValueError, match="positive"):
        search(env, image_key=RED, n_results=0)


def test_missing_image_embeddings_are_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(image_index, "load_embedding_function", lambda device: FakeClip())
    with pytest.raises(ValueError, match="no image embeddings"):
        similar.similar_images(db_path=tmp_path / "empty", device="cpu", image_key=RED)


def test_images_built_with_another_model_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(image_index, "load_embedding_function", lambda device: FakeClip())
    db = chromadb.PersistentClient(path=str(tmp_path / "other"))
    db.create_collection("images", embedding_function=None, metadata={"model": "other/model"})
    with pytest.raises(ValueError, match="other/model"):
        similar.similar_images(db_path=tmp_path / "other", device="cpu", image_key=RED)


# ---- saving the results to look at them ----------------------------------------------------------------------------


@pytest.fixture
def storage(monkeypatch, s3):
    monkeypatch.setattr(similar, "load_storage_config", lambda: {"buckets": {"trusted": TRUSTED}})
    monkeypatch.setattr(similar, "create_minio_client", lambda: s3)
    for key in COLORS:
        s3.put(TRUSTED, key, f"picture of {key}".encode())
    return s3


def test_results_are_downloaded_in_rank_order(storage, tmp_path):
    hits = [{"id": GREEN}, {"id": RED}]
    assert similar.save_results(hits, tmp_path / "out") == 0
    names = sorted(path.name for path in (tmp_path / "out").iterdir())
    assert names == ["01_rawg$games$2$18-10-2026.jpg", "02_rawg$games$1$18-10-2026.jpg"]
    assert (tmp_path / "out" / names[0]).read_bytes() == f"picture of {GREEN}".encode()


def test_one_failed_download_does_not_stop_the_others(storage, tmp_path):
    storage.failures[("get_object", GREEN)] = ConnectionError("network down")
    assert similar.save_results([{"id": RED}, {"id": GREEN}, {"id": BLUE}], tmp_path / "out") == 1
    assert len(list((tmp_path / "out").iterdir())) == 2


# ---- command line ----------------------------------------------------------------------------------------------------


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["image_to_image.py", *argv])
    similar.main()


def test_main_prints_the_ranked_images_for_a_file(env, monkeypatch, capsys):
    query = write_image(env.dir / "mine.png", color=(200, 30, 30))
    run_main(monkeypatch, "--image_file", str(query), "--db_path", str(env.db_path), "--results", "2")
    output = capsys.readouterr().out
    assert f" 1. {RED}" in output and " 2. " in output and " 3. " not in output


def test_main_can_save_the_results_and_exits_cleanly(env, storage, monkeypatch, tmp_path):
    out = tmp_path / "look"
    with pytest.raises(SystemExit) as raised:
        run_main(monkeypatch, "--image_key", RED, "--db_path", str(env.db_path), "--results", "2",
                 "--save_to", str(out))
    assert raised.value.code == 0
    assert len(list(out.iterdir())) == 2


def test_main_turns_a_bad_query_into_a_clean_exit(env, monkeypatch):
    with pytest.raises(SystemExit) as raised:
        run_main(monkeypatch, "--image_file", str(env.dir / "nope.png"), "--db_path", str(env.db_path))
    assert "not found" in str(raised.value)


def test_main_requires_a_query(env, monkeypatch):
    with pytest.raises(SystemExit) as raised:
        run_main(monkeypatch, "--db_path", str(env.db_path))
    assert raised.value.code == 2
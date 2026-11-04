from io import BytesIO
import hashlib

from PIL import Image
import pytest

from src.app import visual_search as service
from src.exploitation.jina_clip import DIMENSION, MODEL_ID, MODEL_REVISION


VECTOR = [1.] + [0.] * (DIMENSION - 1)


class Encoder:
    dimension = DIMENSION
    settings = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION, "code_revision": "pinned",
                "dimension": DIMENSION, "normalization": "L2", "representation": "full_description"}

    def encode_query(self, text):
        self.query = text
        return VECTOR


class Collection:
    configuration = {"hnsw": {"space": "cosine"}}

    def __init__(self, modality):
        configure = service.image_configuration if modality == "image" else lambda encoder: service.text_configuration(encoder, False)
        self.fp = configure(Encoder())[1]
        self.metadata = {"config_sha256": self.fp, "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
                         "dimension": DIMENSION, "normalization": "L2"}
        self.rows = [{"asset_id": f"asset_{n}", "game_id": game, "collection_date": day,
                      "model_id": MODEL_ID, "model_revision": MODEL_REVISION, "dimension": DIMENSION,
                      "modality": modality, "config_sha256": self.fp}
                     for n, (game, day) in enumerate([(1, "2026-10-20"), (1, "2026-11-01"), (2, "2026-11-01")])]

    def get(self, **kwargs):
        return {"ids": ["a", "b", "c"], "metadatas": self.rows}

    def query(self, **kwargs):
        assert kwargs["query_embeddings"] == [VECTOR]
        assert kwargs["n_results"] == 3
        return {"ids": [["a", "b", "c"]], "documents": [["Old", "Full description", "Other game"]],
                "metadatas": [self.rows], "distances": [[0., .2, .1]]}


def test_text_to_image_uses_reference_query_encoding():
    encoder = Encoder()
    results = service.search_images(Collection("image"), encoder, text="a western landscape", top_k=2)
    assert encoder.query == "a western landscape"
    assert [hit["id"] for hit in results] == ["a", "c"]


def test_image_to_image_excludes_reference_and_ranks_unique_assets(monkeypatch):
    monkeypatch.setattr(service, "encode_images", lambda encoder, images: [VECTOR])
    results = service.search_images(Collection("image"), Encoder(), image=Image.new("RGB", (5, 5)), exclude_ids=["a"])
    assert [hit["id"] for hit in results] == ["c", "b"]


def test_image_query_retrieves_both_modalities_and_latest_unique_games(monkeypatch):
    calls = []
    def encode(encoder, images):
        calls.append(images)
        return [VECTOR]
    monkeypatch.setattr(service, "encode_images", encode)
    results = service.search_from_image(Collection("text"), Collection("image"), Encoder(),
                                       Image.new("RGB", (5, 5)), targets=["text", "image"], top_k=3)
    assert len(calls) == 1
    assert [hit["game_id"] for hit in results["text"]] == [2, 1]
    assert results["text"][1]["text"] == "Full description"
    assert len(results["image"]) == 3


def test_text_query_retrieves_both_modalities_with_one_vector():
    result = service.search_from_text(Collection("text"), Collection("image"), Encoder(), "puzzles", targets=["text", "image"])
    assert set(result) == {"text", "image"}


@pytest.mark.parametrize("problem", ["model", "revision", "dimension", "normalization", "metric", "row"])
def test_incompatible_collections_and_rows_are_rejected(problem):
    collection = Collection("image")
    if problem == "metric":
        collection.configuration = {"hnsw": {"space": "l2"}}
    elif problem == "row":
        collection.rows[0]["model_revision"] = "different"
    else:
        key = {"model": "config_sha256", "revision": "model_revision"}.get(problem, problem)
        collection.metadata[key] = "wrong"
    with pytest.raises(ValueError):
        service.search_images(collection, Encoder(), text="query")


@pytest.mark.parametrize("query", ["", "   ", "x" * 4001])
def test_invalid_text_query(query):
    with pytest.raises(ValueError):
        service.search_from_text(None, Collection("image"), Encoder(), query, targets=["image"])


def test_invalid_query_vector_and_missing_database(tmp_path):
    with pytest.raises(ValueError, match="vector"):
        service.rank_vector(Collection("image"), [float("nan")] * DIMENSION, modality="image")
    with pytest.raises(ValueError):
        service.connect_images(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_source_images_are_verified_and_bad_uploads_rejected():
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    body = buffer.getvalue()
    class Client:
        def get_object(self, **kwargs):
            return {"Body": BytesIO(body)}
    metadata = {"trusted_bucket": "trusted", "trusted_object_key": "images/test.png",
                "trusted_object_sha256": hashlib.sha256(body).hexdigest()}
    assert service.load_image(Client(), metadata, "trusted").mode == "RGB"
    metadata["trusted_object_sha256"] = "changed"
    with pytest.raises(ValueError, match="changed"):
        service.load_image(Client(), metadata, "trusted")
    metadata["trusted_bucket"] = "landing"
    with pytest.raises(ValueError, match="Trusted"):
        service.load_image(Client(), metadata, "trusted")
    for invalid in (b"", b"not an image", b"x" * (20 * 1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            service.decode_upload(invalid)

import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.exploitation.text import build_embeddings as embeddings


class Encoder:
    context_length = 8192
    dimension = 1024
    settings = {"model_id": embeddings.MODEL_ID, "model_revision": embeddings.MODEL_REVISION,
                "code_revision": "pinned-code", "dimension": 1024}

    def __init__(self):
        self.calls = []

    def validate_text(self, text):
        if len(text) > self.context_length:
            raise ValueError("Text exceeds the token limit")
        return len(text)

    def encode(self, texts):
        self.calls.extend(texts)
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in texts]


class Collection:
    def __init__(self):
        self.rows = {}
        self.writes = 0
        self.fail_upsert = False
        self.corrupt = False

    def get(self, ids=None, where=None, include=None):
        selected = [key for key in self.rows if (ids is None or key in ids)
                    and (where is None or all(self.rows[key][1].get(name) == value for name, value in where.items()))]
        return {"ids": selected, "documents": [self.rows[key][0] for key in selected],
                "metadatas": [self.rows[key][1] for key in selected], "embeddings": [self.rows[key][2] for key in selected]}

    def upsert(self, ids, documents, metadatas, embeddings):
        if self.fail_upsert:
            raise RuntimeError("failed upsert")
        self.writes += 1
        for key, document, metadata, vector in zip(ids, documents, metadatas, embeddings):
            self.rows[key] = (document, metadata, [0.0, 1.0] + [0.0] * (len(vector) - 2) if self.corrupt else vector)

    def delete(self, ids):
        self.writes += 1
        for key in ids:
            self.rows.pop(key, None)


def asset(text="A description with accents àé and punctuation!", day="2026-10-20", revision="revision"):
    return embeddings.Asset(3498, day, text, "3.trusted-zone", f"texts/rawg/collection_date={day}/game_details.parquet", revision, 0)


@pytest.mark.parametrize("text", ["Hello", "àé中文 🎮\n\nHello   world! " * 20, "x" * 800])
def test_full_description_is_one_vector_without_content_loss(text):
    encoder, collection = Encoder(), Collection()
    assert embeddings.run([asset(text)], encoder, collection, "model", batch_size=2) == 0
    assert len(collection.rows) == 1
    document, metadata, vector = next(iter(collection.rows.values()))
    assert document == text and len(vector) == 1024
    assert metadata["representation"] == "full_description"
    assert metadata["model_revision"] == embeddings.MODEL_REVISION
    assert metadata["token_count"] == len(text)
    assert "chunk_index" not in metadata


def test_oversize_description_preserves_previous_data_and_other_games_continue():
    encoder, collection = Encoder(), Collection()
    assert embeddings.run([asset("Original")], encoder, collection, "model", batch_size=2) == 0
    original, writes = dict(collection.rows), collection.writes
    assert embeddings.run([asset("x" * 8193)], encoder, collection, "model", batch_size=2) == 1
    assert collection.rows == original and collection.writes == writes
    assert embeddings.run([asset("x" * 8193), asset("Valid", "2026-10-22")], encoder, collection, "model", batch_size=2) == 1
    assert len(collection.rows) == 2


def test_incremental_update_reuses_vectors_and_removes_obsolete_chunks():
    encoder, collection = Encoder(), Collection()
    fingerprint = "same-model"
    assert embeddings.run([asset()], encoder, collection, fingerprint, batch_size=2) == 0
    rows, writes, calls = dict(collection.rows), collection.writes, list(encoder.calls)
    assert len(rows) == 1
    assert embeddings.run([asset()], encoder, collection, fingerprint, batch_size=2) == 0
    assert collection.writes == writes and encoder.calls == calls
    # A changed Parquet hash updates provenance without recomputing the same text.
    assert embeddings.run([asset(revision="new-revision")], encoder, collection, fingerprint, batch_size=2) == 0
    assert encoder.calls == calls
    assert embeddings.run([asset("Short")], encoder, collection, fingerprint, batch_size=2) == 0
    assert len(collection.rows) == 1 and set(rows).isdisjoint(collection.rows)
    document, metadata, vector = next(iter(collection.rows.values()))
    assert document == "Short" and metadata["text_sha256"] == embeddings.sha256("Short")
    assert metadata["game_id"] == 3498 and metadata["modality"] == "text"


def test_same_text_on_another_date_keeps_history_and_reuses_embedding_in_run():
    encoder, collection = Encoder(), Collection()
    assert embeddings.run([asset("Short"), asset("Short", "2026-10-22")], encoder, collection, "model", batch_size=2) == 0
    assert encoder.calls == ["Short"]
    assert len(collection.rows) == 2
    assert {metadata["collection_date"] for _, metadata, _ in collection.rows.values()} == {"2026-10-20", "2026-10-22"}


def test_failed_publication_keeps_previous_capture_until_retry():
    encoder, collection = Encoder(), Collection()
    assert embeddings.run([asset()], encoder, collection, "model", batch_size=2) == 0
    old_ids = set(collection.rows)
    collection.fail_upsert = True
    assert embeddings.run([asset("New")], encoder, collection, "model", batch_size=2) == 1
    assert old_ids <= set(collection.rows)
    collection.fail_upsert = False
    assert embeddings.run([asset("New")], encoder, collection, "model", batch_size=2) == 0
    assert len(collection.rows) == 1 and old_ids.isdisjoint(collection.rows)


def test_wrong_vector_is_detected_before_old_vectors_are_deleted():
    encoder, collection = Encoder(), Collection()
    assert embeddings.run([asset()], encoder, collection, "model", batch_size=2) == 0
    old_ids = set(collection.rows)
    collection.corrupt = True
    assert embeddings.run([asset("Different")], encoder, collection, "model", batch_size=2) == 1
    assert old_ids <= set(collection.rows)


@pytest.mark.parametrize("vector", [
    [0.0] * 1024, [float("nan")] + [0.0] * 1023,
    [float("inf")] + [0.0] * 1023, [1.0, 0.0],
])
def test_invalid_generated_vectors_are_never_published(vector):
    encoder, collection = Encoder(), Collection()
    encoder.encode = lambda texts: [vector for _ in texts]
    assert embeddings.run([asset()], encoder, collection, "model", batch_size=2) == 1
    assert not collection.rows and collection.writes == 0


def test_empty_description_removes_stale_vectors_only_for_its_capture():
    encoder, collection = Encoder(), Collection()
    assert embeddings.run([asset("Short"), asset("Short", "2026-10-22")], encoder, collection, "model", batch_size=2) == 0
    assert embeddings.run([asset("")], encoder, collection, "model", batch_size=2) == 0
    assert len(collection.rows) == 1
    assert next(iter(collection.rows.values()))[1]["collection_date"] == "2026-10-22"


class S3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


def seed(client, day="2026-10-20", ids=(3498,)):
    table = pa.table({"id": list(ids), "description_clean": ["Only embed this description"] * len(ids), "name": ["Do not embed this name"] * len(ids)})
    stream = pa.BufferOutputStream()
    pq.write_table(table, stream)
    body = stream.getvalue().to_pybytes()
    key = f"texts/rawg/collection_date={day}/game_details.parquet"
    client.objects[key] = body
    client.objects[f"texts/rawg/manifests/collection_date={day}.json"] = json.dumps({"outputs": {key: embeddings.sha256(body)}, "rows": len(ids)}).encode()
    return key


def test_verified_trusted_input_and_latest_selection(monkeypatch):
    client = S3()
    seed(client)
    seed(client, "2026-10-22")
    monkeypatch.setattr(embeddings, "list_object_keys", lambda c, b, p: list(c.objects))
    storage = {"buckets": {"trusted": "3.trusted-zone"}, "trusted": {"texts_prefix": "texts"}}
    assets = embeddings.load_assets(client, storage)
    assert len(assets) == 2 and all(item.text == "Only embed this description" for item in assets)
    assert [item.day for item in embeddings.load_assets(client, storage, latest_only=True)] == ["2026-10-22"]
    client.objects[seed(client)] = b"corrupted"
    with pytest.raises(ValueError, match="hash"):
        embeddings.load_assets(client, storage)


def test_conflicting_duplicate_ids_fail_before_indexing(monkeypatch):
    client = S3()
    seed(client, ids=(3498, 3498))
    monkeypatch.setattr(embeddings, "list_object_keys", lambda c, b, p: list(c.objects))
    storage = {"buckets": {"trusted": "3.trusted-zone"}, "trusted": {"texts_prefix": "texts"}}
    with pytest.raises(ValueError, match="same game"):
        embeddings.load_assets(client, storage)


def test_configuration_changes_have_different_fingerprints():
    encoder = Encoder()
    _, first = embeddings.configuration(encoder, False)
    _, latest = embeddings.configuration(encoder, True)
    encoder.settings = {**encoder.settings, "model_revision": "different"}
    _, second = embeddings.configuration(encoder, False)
    assert len({first, latest, second}) == 3


def test_real_chroma_persistence_cosine_and_model_guard(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    path = str(tmp_path / "chroma")
    encoder = Encoder()
    settings, fingerprint = embeddings.configuration(encoder, False)
    db = chromadb.PersistentClient(path=path)
    collection = embeddings.open_collection(db, "texts_jina_clip_v2", settings, fingerprint)
    assert embeddings.run([asset("Short")], encoder, collection, fingerprint, batch_size=2) == 0
    reopened = chromadb.PersistentClient(path=path).get_collection("texts_jina_clip_v2", embedding_function=None)
    assert reopened.count() == 1
    result = reopened.query(query_embeddings=[[1.0] + [0.0] * 1023], n_results=1, include=["documents", "distances"])
    assert result["documents"] == [["Short"]] and result["distances"][0][0] == pytest.approx(0)
    with pytest.raises(ValueError, match="different model/configuration"):
        embeddings.open_collection(db, "texts_jina_clip_v2", {**settings, "checkpoint": "new"}, "new-config")


def test_encoder_preserves_full_text_and_explicitly_disables_truncation(monkeypatch):
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    from src.exploitation.jina_clip import JinaEncoder, MAX_TOKENS, QUERY_PREFIX
    class Tokenizer:
        def encode(self, text, **kwargs):
            assert kwargs == {"add_special_tokens": True, "truncation": False}
            return list(range(len(text)))
    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", lambda *args, **kwargs: Tokenizer())
    encoder = JinaEncoder()
    assert encoder.model is None
    calls = []
    class Model:
        def encode_text(self, texts, **kwargs):
            calls.append((texts, kwargs))
            return torch.ones(len(texts), 1024)
    monkeypatch.setattr(encoder, "load_model", Model)
    text = "Complete description. " * 50
    vector = encoder.encode([text])[0]
    assert embeddings.valid_vector(vector, 1024)
    assert calls[-1][0] == [text]
    assert calls[-1][1]["truncation"] is False and calls[-1][1]["max_length"] is None
    assert calls[-1][1]["truncate_dim"] is None and calls[-1][1]["task"] is None
    assert len(encoder.encode_query("Find a game")) == 1024
    assert calls[-1][1]["task"] == "retrieval.query"
    with pytest.raises(ValueError):
        encoder.encode_query("x" * (MAX_TOKENS - len(QUERY_PREFIX) + 1))
    with pytest.raises(ValueError):
        encoder.encode(["x" * (MAX_TOKENS + 1)])


def test_jina_collection_does_not_modify_legacy_clip_vectors(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    db = chromadb.PersistentClient(path=str(tmp_path / "database"))
    old = db.create_collection("texts_clip", embedding_function=None)
    old.add(ids=["old"], documents=["Old CLIP document"], embeddings=[[1.0] + [0.0] * 511])
    before = old.get(include=["documents", "embeddings"])
    encoder = Encoder()
    settings, fingerprint = embeddings.configuration(encoder, False)
    new = embeddings.open_collection(db, embeddings.COLLECTION, settings, fingerprint)
    assert embeddings.run([asset()], encoder, new, fingerprint, batch_size=2) == 0
    after = old.get(include=["documents", "embeddings"])
    assert after["ids"] == before["ids"] and after["documents"] == before["documents"]
    assert (after["embeddings"] == before["embeddings"]).all()
    assert new.count() == old.count() == 1

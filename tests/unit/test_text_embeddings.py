import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.exploitation.text import build_embeddings as embeddings


class Encoder:
    budget = 12
    dimension = 3
    settings = {"model_name": "ViT-B-32", "checkpoint": "laion2b_s34b_b79k"}

    def __init__(self):
        self.calls = []

    def count_tokens(self, text):
        return len(text)  # force small exact fragments, including Unicode

    def encode(self, texts):
        self.calls.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]


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
            self.rows[key] = (document, metadata, [0.0, 1.0, 0.0] if self.corrupt else vector)

    def delete(self, ids):
        self.writes += 1
        for key in ids:
            self.rows.pop(key, None)


def asset(text="A description with accents àé and punctuation!", day="2026-10-20", revision="revision"):
    return embeddings.Asset(3498, day, text, "3.trusted-zone", f"texts/rawg/collection_date={day}/game_details.parquet", revision, 0)


@pytest.mark.parametrize("text", ["Hello", "àé中文 🎮\n\nHello   world! " * 20, "x" * 80, "line\n" * 20 + "  "])
def test_chunking_preserves_exact_text_offsets_and_token_budget(text):
    encoder = Encoder()
    chunks = embeddings.split_text(text, encoder.count_tokens, encoder.budget)
    assert "".join(chunk.text for chunk in chunks) == text
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    for index, chunk in enumerate(chunks):
        assert chunk.text == text[chunk.start:chunk.end]
        assert chunk.tokens == encoder.count_tokens(chunk.text) <= encoder.budget
        assert index == 0 or chunks[index - 1].end == chunk.start


def test_blank_text_and_invalid_token_budget():
    assert embeddings.split_text("  \n", len, 10) == []
    with pytest.raises(ValueError):
        embeddings.split_text("Text", len, 0)
    with pytest.raises(ValueError):
        embeddings.split_text("字", lambda text: 5 * len(text), 2)


def test_incremental_update_reuses_vectors_and_removes_obsolete_chunks():
    encoder, collection = Encoder(), Collection()
    fingerprint = "same-model"
    assert embeddings.run([asset()], encoder, collection, fingerprint, batch_size=2) == 0
    rows, writes, calls = dict(collection.rows), collection.writes, list(encoder.calls)
    assert len(rows) > 1
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
    encoder.settings = {**encoder.settings, "checkpoint": "different"}
    _, second = embeddings.configuration(encoder, False)
    assert len({first, latest, second}) == 3


def test_real_chroma_persistence_cosine_and_model_guard(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    path = str(tmp_path / "chroma")
    encoder = Encoder()
    settings, fingerprint = embeddings.configuration(encoder, False)
    db = chromadb.PersistentClient(path=path)
    collection = embeddings.open_collection(db, "texts_clip", settings, fingerprint)
    assert embeddings.run([asset("Short")], encoder, collection, fingerprint, batch_size=2) == 0
    reopened = chromadb.PersistentClient(path=path).get_collection("texts_clip", embedding_function=None)
    assert reopened.count() == 1
    result = reopened.query(query_embeddings=[[1, 0, 0]], n_results=1, include=["documents", "distances"])
    assert result["documents"] == [["Short"]] and result["distances"][0][0] == pytest.approx(0)
    with pytest.raises(ValueError, match="different model/configuration"):
        embeddings.open_collection(db, "texts_clip", {**settings, "checkpoint": "new"}, "new-config")


def test_real_openclip_tokenizer_without_weights():
    pytest.importorskip("open_clip")
    encoder = embeddings.OpenClipEncoder()
    text = "Text llarg amb accents àé, 中文 i símbols 🎮. " * 100
    chunks = embeddings.split_text(text, encoder.count_tokens, encoder.budget)
    assert encoder.model is None and len(chunks) > 1
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(encoder.count_tokens(chunk.text) + 2 <= encoder.context_length for chunk in chunks)

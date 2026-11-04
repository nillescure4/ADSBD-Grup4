"""CLAP source coverage, incremental updates and verified game-level retrieval."""

from dataclasses import replace
import re

import pytest

from src.exploitation.text import build_clap_embeddings as pipeline
from src.app.clap_text_search import search_clap_descriptions
from tests.unit.test_text_embeddings import Collection, asset


class Tokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def encode(self, text, add_special_tokens=True):
        return list(range(len(re.findall(r"\S+", text)) + (2 if add_special_tokens else 0)))

    def __call__(self, text, **kwargs):
        return {"offset_mapping": [(word.start(), word.end()) for word in re.finditer(r"\S+", text)]}


class Encoder:
    tokenizer = Tokenizer()

    def __init__(self):
        self.calls = []

    def encode(self, text):
        self.calls.append(text)
        return [1.] + [0.] * 511


@pytest.mark.parametrize("text", ["An adventure.", "àé中文 🎮\n\nHello   world! " * 200, "word " * 1200])
def test_one_vector_uses_the_full_description_or_its_bounded_source_prefix(text):
    prepared = pipeline.prepare_description(text, Tokenizer())
    assert len(prepared) == 1
    start, end, document = prepared[0]
    assert start == 0 and text[:end] == document
    assert len(Tokenizer().encode(document)) <= 512
    if len(Tokenizer().encode(text)) <= 512:
        assert document == text
    else:
        assert end < len(text) and len(Tokenizer().encode(document)) == 512


def test_incremental_rerun_and_new_capture_reuse_one_vector_and_replace_old_versions():
    encoder, collection = Encoder(), Collection()
    original = asset("A description. " * 100)
    assert pipeline.run([original], encoder, collection)["updated"] == 1
    calls, writes = list(encoder.calls), collection.writes
    assert pipeline.run([original], encoder, collection)["skipped"] == 1
    assert encoder.calls == calls and collection.writes == writes
    newer = replace(original, day="2026-11-02", object_sha256="new-snapshot")
    assert pipeline.run([newer], encoder, collection)["encoded"] == 0
    assert encoder.calls == calls
    assert all(meta["collection_date"] == "2026-11-02" for _, meta, _ in collection.rows.values())
    pipeline.run([replace(newer, text="Changed description")], encoder, collection)
    assert len(collection.rows) == 1
    assert next(iter(collection.rows.values()))[0] == "Changed description"


def test_failed_publication_preserves_previous_vectors():
    encoder, collection = Encoder(), Collection()
    pipeline.run([asset("Original")], encoder, collection)
    old = set(collection.rows)
    collection.corrupt = True
    with pytest.raises(ValueError, match="verify"):
        pipeline.run([asset("Changed")], encoder, collection)
    assert old <= set(collection.rows)


def test_audio_queries_return_each_game_once_and_reject_wrong_model_revision():
    encoder, collection = Encoder(), Collection()
    pipeline.run([asset("Description " * 200), replace(asset("Another game"), game_id=28)], encoder, collection)
    collection.metadata = {"config_sha256": pipeline.FINGERPRINT, "model_id": pipeline.MODEL_ID,
                           "model_revision": pipeline.MODEL_REVISION, "dimension": 512, "normalization": "L2"}
    collection.configuration = {"hnsw": {"space": "cosine"}}
    collection.count = lambda: len(collection.rows)
    def query(**kwargs):
        data = collection.get()
        return {"ids": [data["ids"]], "documents": [data["documents"]], "metadatas": [data["metadatas"]],
                "distances": [[.2 + index * .01 for index in range(len(data["ids"]))]]}
    collection.query = query
    hits = search_clap_descriptions(collection, [1.] + [0.] * 511, top_k=8)
    assert len(hits) == 2 and len({hit["game_id"] for hit in hits}) == 2
    assert hits[0]["score"] == pytest.approx(.8)
    collection.metadata["model_revision"] = "wrong-revision"
    with pytest.raises(ValueError, match="pinned CLAP"):
        search_clap_descriptions(collection, [1.] + [0.] * 511)

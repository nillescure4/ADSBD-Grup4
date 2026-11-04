"""Transient HNSW reload errors are retried without hiding permanent failures."""

from types import SimpleNamespace

import pytest
from chromadb.errors import InternalError

from src.common import chroma_reads


MESSAGE = "Error creating hnsw segment reader: Nothing found on disk"


@pytest.mark.parametrize("method", ["get", "query"])
def test_transient_failure_returns_the_actual_second_read(method, monkeypatch):
    calls, pauses = [], []
    def read(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise InternalError(MESSAGE)
        return {"ids": ["verified"]}
    monkeypatch.setattr(chroma_reads.time, "sleep", pauses.append)
    wrapped = chroma_reads.ReliableReads(SimpleNamespace(**{method: read}, metadata={"model": "pinned"}))
    assert getattr(wrapped, method)(include=["embeddings"]) == {"ids": ["verified"]}
    assert calls == [{"include": ["embeddings"]}] * 2
    assert pauses == [0.5] and wrapped.metadata == {"model": "pinned"}


def test_permanent_reload_failure_is_not_accepted(monkeypatch):
    calls = []
    def read():
        calls.append(1)
        raise InternalError(MESSAGE)
    monkeypatch.setattr(chroma_reads.time, "sleep", lambda *args: None)
    with pytest.raises(InternalError, match="Nothing found"):
        chroma_reads.ReliableReads(SimpleNamespace(get=read)).get()
    assert len(calls) == 3


def test_other_database_errors_are_not_retried(monkeypatch):
    def read():
        raise InternalError("Invalid database schema")
    monkeypatch.setattr(chroma_reads.time, "sleep", lambda *args: pytest.fail("Must not retry"))
    with pytest.raises(InternalError, match="Invalid database schema"):
        chroma_reads.ReliableReads(SimpleNamespace(get=read)).get()

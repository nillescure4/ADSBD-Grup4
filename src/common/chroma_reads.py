"""Bounded retries for Chroma's transient HNSW segment-reload race."""

import logging
import time


LOGGER = logging.getLogger(__name__)


class ReliableReads:
    """Retry only the known transient disk-reader error, never replace results.

    Chroma 1.5.9 can briefly fail after in-process HNSW cache eviction even
    though the index files exist (chroma-core/chroma#7946). Permanent errors
    still propagate after three attempts; validation is never bypassed.
    """

    def __init__(self, collection):
        self.collection = collection

    def __getattr__(self, name):
        return getattr(self.collection, name)

    def read(self, method, *args, **kwargs):
        from chromadb.errors import InternalError
        for attempt in range(3):
            try:
                return getattr(self.collection, method)(*args, **kwargs)
            except InternalError as exc:
                if "Error creating hnsw segment reader: Nothing found on disk" not in str(exc) or attempt == 2:
                    raise
                LOGGER.warning("Retrying a transient Chroma HNSW reload error (%d/2)", attempt + 1)
                time.sleep(0.5 * (attempt + 1))

    def get(self, *args, **kwargs):
        return self.read("get", *args, **kwargs)

    def query(self, *args, **kwargs):
        return self.read("query", *args, **kwargs)

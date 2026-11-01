"""Shared fixtures for the image pipeline tests: an in-memory S3 client and tiny image builders."""

from __future__ import annotations

import hashlib
import io
from collections import Counter
from datetime import datetime, timezone

import numpy as np
import pytest
from botocore.exceptions import ClientError
from PIL import Image

DEFAULT_MODIFIED = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
PAGE_SIZE = 2  # tiny pages, so every listing in the tests exercises pagination


class FakeS3Client:
    """Minimal in-memory S3 client with only the calls the image pipeline makes."""

    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}
        self.calls: Counter = Counter()
        self.failures: dict[tuple[str, str], Exception] = {}  # (method, key) -> error to raise
        self.corrupt_copies = False                           # copy_object stores damaged data

    # ---- helpers for the tests -------------------------------------------------
    def put(self, bucket: str, key: str, data: bytes, last_modified=DEFAULT_MODIFIED) -> None:
        self.objects[(bucket, key)] = {"data": data, "last_modified": last_modified, "content_type": None}

    def keys(self, bucket: str, prefix: str = "") -> list[str]:
        return sorted(key for b, key in self.objects if b == bucket and key.startswith(prefix))

    def data(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]["data"]

    def content_type(self, bucket: str, key: str) -> str | None:
        return self.objects[(bucket, key)]["content_type"]

    # ---- S3 API ------------------------------------------------------------------
    def _check(self, method: str, key: str) -> None:
        self.calls[method] += 1
        error = self.failures.get((method, key))
        if error is not None:
            raise error

    @staticmethod
    def _etag(data: bytes) -> str:
        return '"' + hashlib.md5(data).hexdigest() + '"'

    def _get(self, bucket: str, key: str, operation: str) -> dict:
        try:
            return self.objects[(bucket, key)]
        except KeyError:
            code = "NoSuchKey" if operation == "GetObject" else "404"
            raise ClientError({"Error": {"Code": code}}, operation) from None

    def head_bucket(self, Bucket):
        self.calls["head_bucket"] += 1

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._check("put_object", Key)
        self.objects[(Bucket, Key)] = {
            "data": bytes(Body), "last_modified": DEFAULT_MODIFIED, "content_type": ContentType,
        }

    def get_object(self, Bucket, Key):
        self._check("get_object", Key)
        return {"Body": io.BytesIO(self._get(Bucket, Key, "GetObject")["data"])}

    def head_object(self, Bucket, Key):
        self._check("head_object", Key)
        obj = self._get(Bucket, Key, "HeadObject")
        return {"ContentLength": len(obj["data"]), "ETag": self._etag(obj["data"]),
                "LastModified": obj["last_modified"]}

    def copy_object(self, Bucket, Key, CopySource):
        self._check("copy_object", Key)
        source = self._get(CopySource["Bucket"], CopySource["Key"], "CopyObject")
        data = b"X" + source["data"] if self.corrupt_copies else source["data"]
        self.objects[(Bucket, Key)] = {**source, "data": data}

    def delete_object(self, Bucket, Key):
        self._check("delete_object", Key)
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, name):
        assert name == "list_objects_v2", name
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix=""):
                client.calls["list_objects_v2"] += 1
                keys = client.keys(Bucket, Prefix)
                if not keys:
                    yield {}  # S3 returns a page without "Contents" when nothing matches
                for start in range(0, len(keys), PAGE_SIZE):
                    yield {"Contents": [
                        {"Key": key,
                         "ETag": client._etag(client.objects[(Bucket, key)]["data"]),
                         "Size": len(client.objects[(Bucket, key)]["data"]),
                         "LastModified": client.objects[(Bucket, key)]["last_modified"]}
                        for key in keys[start:start + PAGE_SIZE]
                    ]}

        return Paginator()


@pytest.fixture
def s3() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def image_bytes():
    """Build a small real image file in memory.

    Without ``seed`` the image is a solid ``color`` (default black). With ``seed`` it is a
    reproducible blocky random image: sharp, high contrast and different for every seed.
    Extra keyword arguments go to ``Image.save`` (for example ``transparency`` or ``exif``).
    """

    def build(*, mode="RGB", size=(64, 48), fmt="PNG", color=None, seed=None, **save_kwargs) -> bytes:
        if seed is not None:
            rng = np.random.default_rng(seed)
            blocks = rng.integers(0, 256, (size[1] // 8 + 1, size[0] // 8 + 1, 3), dtype=np.uint8)
            pixels = np.kron(blocks, np.ones((8, 8, 1), dtype=np.uint8))[: size[1], : size[0]]
            image = Image.fromarray(pixels).convert(mode)
        elif color is not None:
            image = Image.new(mode, size, color)
        else:
            image = Image.new(mode, size)
        buffer = io.BytesIO()
        image.save(buffer, format=fmt, **save_kwargs)
        return buffer.getvalue()

    return build


@pytest.fixture
def png_from_array():
    """Encode a uint8 numpy array (H x W or H x W x 3) as a lossless PNG."""

    def build(array) -> bytes:
        buffer = io.BytesIO()
        Image.fromarray(np.asarray(array, dtype=np.uint8)).save(buffer, format="PNG")
        return buffer.getvalue()

    return build
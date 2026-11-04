from io import BytesIO
import json

from botocore.exceptions import ClientError
import pytest

from src.exploitation.images.build_embeddings import load_assets
from src.exploitation.images import build_embeddings as pipeline


KEY = "images/rawg$rawg_images$shot_01_hash$01-11-2026.jpg"


@pytest.mark.parametrize("extension", ["", ".jpg", ".png", ".webp", ".tiff"])
def test_canonical_and_legacy_image_keys_preserve_date_and_identity(extension):
    key = KEY.removesuffix(".jpg") + extension + ".jpg"
    result = pipeline.parse_key(key)
    assert result["collection_date"] == "2026-11-01"
    assert result["image_id"] == "shot_01_hash"
    assert result["source"] == "rawg"


class Client:
    def __init__(self, record=None):
        self.record = record

    def get_object(self, **kwargs):
        if self.record is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO((json.dumps(self.record) + "\n").encode())}

    def get_paginator(self, name):
        return self

    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": KEY, "ETag": '"etag"'}]}]


def test_game_id_is_not_guessed_without_explicit_provenance():
    [asset] = load_assets(Client(), {"buckets": {"trusted": "trusted"}})
    assert "game_id" not in asset.provenance


def test_explicit_game_provenance_matches_exact_image_version():
    record = {"object_key": KEY, "etag": "etag", "game_id": 28, "game_name": "Red Dead Redemption 2"}
    [asset] = load_assets(Client(record), {"buckets": {"trusted": "trusted"}})
    assert asset.provenance["game_id"] == 28
    assert asset.provenance["game_name"] == "Red Dead Redemption 2"
    record["etag"] = "changed"
    with pytest.raises(ValueError, match="Stale"):
        load_assets(Client(record), {"buckets": {"trusted": "trusted"}})


def test_changed_game_provenance_is_refreshed_even_for_unchanged_pixels(monkeypatch):
    asset = pipeline.Asset("trusted", KEY, "etag", {"game_id": 28, "game_name": "Correct name"})
    item = pipeline.record_id(asset, "config")
    class Collection:
        def get(self, **kwargs):
            return {"ids": [item], "metadatas": [{"asset_id": asset.id, "config_sha256": "config", "game_id": 28, "game_name": "Old name"}],
                    "embeddings": [[1., 0.]]}
        def count(self):
            return 1
    class Encoder:
        dimension = 2
    refreshed = []
    def sync(client, collection, encoder, batch, fingerprint, stored):
        refreshed.extend(batch)
        return len(batch), 0
    monkeypatch.setattr(pipeline, "sync_batch", sync)
    assert pipeline.run(None, [asset], Encoder(), Collection(), "config", batch_size=1, prune=False) == 0
    assert refreshed == [asset]

import hashlib
import io
import json

from botocore.exceptions import ClientError
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.formatting import text_to_formatted_zone as formatting


LANDING = "1.landing-zone"
FORMATTED = "2.formatted-zone"


class ObjectStore:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.corrupt_upload = False

    def head_bucket(self, **kwargs):
        pass

    def get_object(self, Bucket, Key):
        try:
            return {"Body": io.BytesIO(self.objects[Bucket, Key])}
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.writes.append((Bucket, Key))
        self.objects[Bucket, Key] = b"corrupted" if self.corrupt_upload else Body


@pytest.fixture
def store(monkeypatch):
    client = ObjectStore()
    monkeypatch.setattr(formatting, "create_minio_client", lambda: client)
    monkeypatch.setattr(formatting, "list_object_keys", lambda c, b, p: [
        key for bucket, key in c.objects if bucket == b and key.startswith(p)
    ])
    return client


def add_capture(store, game_id, day="20-10-2026", **changes):
    payload = {
        "id": game_id, "name": "Joc amb accents: àé", "description": "<p>Original</p>",
        "description_raw": "Original\nSegona línia", "rating": 4.5, "tba": False,
        "genres": [{"id": 1, "name": "Action"}], "platforms": [], "metacritic": None,
        **changes,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    prefix = "persistent/texts/rawg/"
    key = f"{prefix}rawg$game_details_{game_id}${day}.json"
    date = formatting.datetime.strptime(day, "%d-%m-%Y").date().isoformat()
    record = {
        "source": "rawg", "source_game_id": game_id, "bucket": LANDING,
        "persistent_object_key": key, "ingested_at_utc": date + "T12:00:00+00:00",
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    store.objects[LANDING, key] = body
    store.objects[LANDING, f"{prefix}manifests/rawg$text_sources_{game_id}${day}.jsonl"] = json.dumps(record).encode()
    return payload, key


def read_partition(store, day="2026-10-20"):
    key = f"texts/rawg/collection_date={day}/game_details.parquet"
    return pq.ParquetFile(pa.BufferReader(store.objects[FORMATTED, key])).read()


def test_format_preserves_content_types_and_provenance(store):
    payload, key = add_capture(store, 3498)
    originals = dict(store.objects)
    assert formatting.format_texts() == 0
    table = read_partition(store)
    assert table.num_rows == 1 and table.num_columns == 54
    assert not {"source", "ingested_at_utc", "source_object_key", "source_sha256"} & set(table.column_names)
    row = table.to_pylist()[0]
    for name, value in payload.items():
        actual = json.loads(row[name]) if name in formatting.JSON_FIELDS and row[name] is not None else row[name]
        assert actual == value
    assert table.schema.field("metacritic").type == pa.int64()
    checkpoint = json.loads(store.objects[FORMATTED, "texts/rawg/manifests/collection_date=2026-10-20.json"])
    provenance = checkpoint["row_provenance"][0]
    assert provenance["source_object_key"] == key
    assert provenance["source_sha256"] == hashlib.sha256(originals[LANDING, key]).hexdigest()
    assert provenance["row_index"] == 0 and provenance["id"] == row["id"]
    assert provenance["ingested_at_utc"] == "2026-10-20T12:00:00+00:00"
    assert all(store.objects[k] == v for k, v in originals.items())


def test_incremental_same_day_new_day_and_idempotency(store):
    add_capture(store, 1)
    assert formatting.format_texts() == 0
    writes = len(store.writes)
    assert formatting.format_texts() == 0
    assert len(store.writes) == writes
    add_capture(store, 2)
    assert formatting.format_texts() == 0
    assert {row["id"] for row in read_partition(store).to_pylist()} == {1, 2}
    old = store.objects[FORMATTED, "texts/rawg/collection_date=2026-10-20/game_details.parquet"]
    add_capture(store, 1, day="21-10-2026", rating=4.6)
    assert formatting.format_texts() == 0
    assert read_partition(store, "2026-10-21").to_pylist()[0]["rating"] == 4.6
    assert store.objects[FORMATTED, "texts/rawg/collection_date=2026-10-20/game_details.parquet"] == old


def test_dry_run_and_new_fields(store):
    add_capture(store, 1, future_field={"nested": [True, 1, "text"]})
    assert formatting.format_texts(dry_run=True) == 0 and not store.writes
    assert formatting.format_texts() == 0
    table = read_partition(store)
    assert json.loads(table.to_pylist()[0]["future_field"]) == {"nested": [True, 1, "text"]}
    assert "future_field" in json.loads(table.schema.metadata[b"json_encoded_fields"])


@pytest.mark.parametrize("failure", ["id", "checksum", "date", "missing_manifest", "type"])
def test_invalid_input_does_not_write_outputs(store, failure):
    _, key = add_capture(store, 1, **({"rating": "wrong"} if failure == "type" else {}))
    record_key = next(k for k in store.objects if k[1].endswith(".jsonl"))
    if failure == "missing_manifest":
        del store.objects[record_key]
    elif failure in {"checksum", "date"}:
        record = json.loads(store.objects[record_key])
        record["sha256" if failure == "checksum" else "ingested_at_utc"] = "wrong" if failure == "checksum" else "2026-10-21T12:00:00+00:00"
        store.objects[record_key] = json.dumps(record).encode()
    elif failure == "id":
        store.objects[LANDING, key] = b'{"id": 2}'
    assert formatting.format_texts() == 1
    assert not store.writes


def test_corrupted_upload_can_be_retried(store):
    add_capture(store, 1)
    store.corrupt_upload = True
    assert formatting.format_texts() == 1
    assert not any(key.endswith(".json") for bucket, key in store.objects if bucket == FORMATTED)
    store.corrupt_upload = False
    assert formatting.format_texts() == 0
    assert read_partition(store).num_rows == 1
    key = (FORMATTED, "texts/rawg/collection_date=2026-10-20/game_details.parquet")
    store.objects[key] = b"damaged later"
    assert formatting.format_texts() == 0
    assert read_partition(store).num_rows == 1


def test_changed_persistent_file_does_not_replace_formatted_history(store):
    add_capture(store, 1)
    assert formatting.format_texts() == 0
    writes = len(store.writes)
    add_capture(store, 1, rating=1.0)
    assert formatting.format_texts() == 1
    assert len(store.writes) == writes
    assert read_partition(store).to_pylist()[0]["rating"] == 4.5


def test_legacy_schema_is_rebuilt_and_manifest_tracks_row_order(store):
    add_capture(store, 9)
    add_capture(store, 1)
    assert formatting.format_texts() == 0
    checkpoint_key = (FORMATTED, "texts/rawg/manifests/collection_date=2026-10-20.json")
    checkpoint = json.loads(store.objects[checkpoint_key])
    checkpoint["format_version"] = 1
    del checkpoint["row_provenance"]
    store.objects[checkpoint_key] = json.dumps(checkpoint).encode()
    writes = len(store.writes)
    assert formatting.format_texts() == 0 and len(store.writes) == writes + 2
    checkpoint = json.loads(store.objects[checkpoint_key])
    table = read_partition(store)
    assert table.num_columns == 54
    assert [row["id"] for row in table.to_pylist()] == [item["id"] for item in checkpoint["row_provenance"]]
    assert [item["row_index"] for item in checkpoint["row_provenance"]] == [0, 1]
    # A damaged row mapping must be repaired even when the input JSONs did not change.
    checkpoint["row_provenance"].reverse()
    store.objects[checkpoint_key] = json.dumps(checkpoint).encode()
    assert formatting.format_texts() == 0 and len(store.writes) == writes + 4

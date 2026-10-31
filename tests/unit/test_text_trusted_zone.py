import hashlib
import io
import json

from botocore.exceptions import ClientError
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.formatting.text_to_formatted_zone import json_to_table
from src.trusted.text_quality import assess_and_clean, clean_description
from src.trusted import text_to_trusted_zone as trusted


def capture(game_id=1, day="2026-10-20", **changes):
    payload = {"id": game_id, "name": "Joc", "description_raw": "Una descripcio prou llarga per provar les regles de qualitat sense avisos.",
               "description": "<p>Descripcio HTML</p>", "genres": [], **changes}
    provenance = {"source": "rawg", "ingested_at_utc": day + "T12:00:00+00:00",
                  "source_object_key": f"persistent/texts/rawg/{game_id}-{day}.json", "source_sha256": "a" * 64}
    return payload, provenance


def provenance_rows(captures):
    return [{"row_index": index, "id": payload["id"], **provenance}
            for index, (payload, provenance) in enumerate(captures)]


@pytest.mark.parametrize(("text", "html", "expected"), [
    ("  Àccents\t i   MAJÚSCULES!\r\n\r\n Paràgraf\x00 següent. ", False, "Àccents i MAJÚSCULES!\n\nParàgraf següent."),
    ("<p>Hola <b>món</b> &amp; amics.</p><p>Segon paràgraf.</p>", True, "Hola món & amics.\n\nSegon paràgraf."),
    ("<style>bad</style><script>bad()</script><p>Conserva això.</p>", True, "Conserva això."),
    ("2 < 3 i 5 > 4", False, "2 < 3 i 5 > 4"),
    ("A&nbsp;&nbsp;B", False, "A B"),
])
def test_conservative_text_cleaning(text, html, expected):
    assert clean_description(text, html=html) == expected


def test_fallback_missing_short_and_original_columns_preserved():
    captures = [
        capture(1, description_raw=None, description="<p>Hola &amp; adeu.</p>"),
        capture(2, description_raw="  ", description=None),
        capture(3, description_raw="<script>bad()</script>", description="<p>Alternativa</p>"),
    ]
    table = json_to_table(captures)
    output, report = assess_and_clean(table, "2026-10-20", row_provenance=provenance_rows(captures))
    retained = [name for name in table.column_names if name not in {"description", "description_raw"}]
    assert output.select(retained).equals(table.select(retained))
    assert output.num_columns == 53
    assert not {"description", "description_raw", "description_clean_source", "description_length", "quality_flags", "quality_status"} & set(output.column_names)
    rows = output.to_pylist()
    assert rows[0]["description_clean"] == "Hola & adeu."
    assert report["row_analysis"][0]["description_clean_source"] == "description"
    assert "short_description" in report["row_analysis"][0]["flags"]
    assert rows[1]["description_clean"] is None and "missing_description" in report["row_analysis"][1]["flags"]
    assert rows[2]["description_clean"] == "Alternativa"
    assert report["corrections"]["html_fallback_rows"] == 2
    assert report["after"]["rows"] == 3
    assert report["after"]["nulls_by_column"]["description_clean"] == 1
    issue = report["issues"][1]
    assert issue["id"] == 2 and issue["collection_date"] == "2026-10-20"
    assert issue["input_row"] == issue["output_row"] == 1
    assert issue["source_object_key"] == captures[1][1]["source_object_key"]
    assert not {"source", "ingested_at_utc", "source_object_key", "source_sha256"} & set(output.column_names)
    markdown = trusted.quality_markdown(report)
    assert "Fila Trusted 2 (entrada 2); ID 2; data 2026-10-20" in markdown


def test_only_exact_duplicates_removed_and_conflicts_flagged():
    first = capture(1)
    captures = [first, first, capture(1, description_raw="Different"), capture(2, description_raw="Different")]
    table = json_to_table(captures)
    output, report = assess_and_clean(table, "2026-10-20", row_provenance=provenance_rows(captures))
    assert output.num_rows == 3
    assert report["corrections"]["exact_duplicates_removed"] == 1
    analysis = report["row_analysis"]
    assert "conflicting_capture" in analysis[0]["flags"]
    assert "conflicting_capture" in analysis[1]["flags"]
    assert "shared_description_across_games" in analysis[1]["flags"]
    assert "shared_description_across_games" in analysis[2]["flags"]
    assert analysis[1]["input_row"] == 2 and analysis[1]["output_row"] == 1


def test_invalid_values_are_kept_and_reported():
    payload, provenance = capture(-1, released="wrong", updated="wrong", rating=float("inf"))
    provenance["ingested_at_utc"] = "bad"
    provenance["source_sha256"] = "bad"
    table = json_to_table([(payload, provenance)])
    table = table.set_column(table.schema.get_field_index("genres"), "genres", pa.array(["broken-json"]))
    output, report = assess_and_clean(table, "2026-10-20", row_provenance=provenance_rows([(payload, provenance)]), expected_sources={})
    assert output.num_rows == 1
    flags = set(report["issues"][0]["flags"])
    assert {"invalid_id", "invalid_release_date", "invalid_updated_timestamp", "invalid_collection_timestamp",
            "invalid_source_sha256", "invalid_json:genres", "nonfinite_number:rating", "provenance_not_in_formatted_manifest"} <= flags
    assert report["after"]["rows_with_warnings"] == 1


class Store:
    def __init__(self):
        self.objects = {}
        self.writes = []
        self.corrupt = False

    def head_bucket(self, **kwargs):
        pass

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Bucket, Key] = b"bad" if self.corrupt else Body
        self.writes.append((Bucket, Key))


@pytest.fixture
def store(monkeypatch):
    store = Store()
    monkeypatch.setattr(trusted, "create_minio_client", lambda: store)
    monkeypatch.setattr(trusted, "list_object_keys", lambda c, b, p: [key for bucket, key in c.objects if bucket == b and key.startswith(p)])
    return store


def add_partition(store, day="2026-10-20", ids=(1,), **changes):
    captures = [capture(i, day, **changes) for i in ids]
    table = json_to_table(captures)
    stream = pa.BufferOutputStream()
    pq.write_table(table, stream)
    body = stream.getvalue().to_pybytes()
    key = f"texts/rawg/collection_date={day}/game_details.parquet"
    store.objects["2.formatted-zone", key] = body
    record = {"output_key": key, "output_sha256": hashlib.sha256(body).hexdigest(), "row_count": table.num_rows,
              "row_index_base": 0, "row_provenance": provenance_rows(captures),
              "inputs": [{"key": provenance["source_object_key"], "sha256": provenance["source_sha256"]} for _, provenance in captures]}
    store.objects["2.formatted-zone", f"texts/rawg/manifests/collection_date={day}.json"] = json.dumps(record).encode()
    return table


def test_two_dates_incremental_updates_and_no_duplicates(store):
    add_partition(store)
    add_partition(store, "2026-10-22")
    originals = dict(store.objects)
    assert trusted.trust_texts(dry_run=True) == 0 and not store.writes
    assert trusted.trust_texts() == 0
    assert len(store.writes) == 11
    writes = len(store.writes)
    assert trusted.trust_texts() == 0 and len(store.writes) == writes
    old = store.objects["3.trusted-zone", "texts/rawg/collection_date=2026-10-22/game_details.parquet"]
    add_partition(store, ids=(1, 2))
    assert trusted.trust_texts() == 0
    assert len(store.writes) == writes + 7
    assert store.objects["3.trusted-zone", "texts/rawg/collection_date=2026-10-22/game_details.parquet"] == old
    table = pq.ParquetFile(pa.BufferReader(store.objects["3.trusted-zone", "texts/rawg/collection_date=2026-10-20/game_details.parquet"])).read()
    assert table.num_rows == 2
    assert all(store.objects[k] == v for k, v in originals.items() if "2026-10-22" in k[1])
    report = json.loads(store.objects["3.trusted-zone", "texts/rawg/quality/collection_date=2026-10-20.json"])
    assert report["before"]["rows"] == report["after"]["rows"] == 2
    assert trusted.trust_texts(min_description_chars=100) == 0
    assert len(store.writes) == writes + 18


def test_corrupted_input_is_rejected_and_outputs_can_be_repaired(store):
    add_partition(store)
    source = ("2.formatted-zone", "texts/rawg/collection_date=2026-10-20/game_details.parquet")
    original = store.objects[source]
    store.objects[source] = b"damaged"
    assert trusted.trust_texts() == 1 and not store.writes
    store.objects[source] = original
    store.corrupt = True
    assert trusted.trust_texts() == 1
    assert not any("manifests/" in k for b, k in store.objects if b == "3.trusted-zone")
    store.corrupt = False
    assert trusted.trust_texts() == 0
    store.objects["3.trusted-zone", "texts/rawg/quality/collection_date=2026-10-20.md"] = b"damaged"
    writes = len(store.writes)
    assert trusted.trust_texts() == 0 and len(store.writes) == writes + 4


def test_partition_errors_do_not_block_other_dates(store):
    add_partition(store)
    add_partition(store, "2026-10-22")
    del store.objects["2.formatted-zone", "texts/rawg/manifests/collection_date=2026-10-20.json"]
    assert trusted.trust_texts() == 1
    assert ("3.trusted-zone", "texts/rawg/collection_date=2026-10-22/game_details.parquet") in store.objects


def test_nan_is_flagged_and_preserved_instead_of_blocking_upload(store):
    add_partition(store, rating=float("nan"))
    assert trusted.trust_texts() == 0
    report = json.loads(store.objects["3.trusted-zone", "texts/rawg/quality/collection_date=2026-10-20.json"])
    assert report["after"]["flag_counts"]["nonfinite_number:rating"] == 1


def test_existing_version_one_is_rebuilt_with_simplified_schema(store):
    original = add_partition(store)
    assert trusted.trust_texts() == 0
    key = ("3.trusted-zone", "texts/rawg/collection_date=2026-10-20/game_details.parquet")
    for name, value in capture()[1].items():
        original = original.append_column(name, pa.array([value]))
    old_table = original.append_column("description_clean", pa.array(["Old text"]))
    for name in ("description_clean_source", "description_length", "quality_flags", "quality_status"):
        old_table = old_table.append_column(name, pa.array(["old diagnostic"]))
    stream = pa.BufferOutputStream()
    pq.write_table(old_table, stream)
    store.objects[key] = stream.getvalue().to_pybytes()
    checkpoint_key = ("3.trusted-zone", "texts/rawg/manifests/collection_date=2026-10-20.json")
    checkpoint = json.loads(store.objects[checkpoint_key])
    checkpoint["input"]["quality_version"] = 1
    checkpoint["outputs"][key[1]] = hashlib.sha256(store.objects[key]).hexdigest()
    store.objects[checkpoint_key] = json.dumps(checkpoint).encode()
    writes = len(store.writes)
    assert trusted.trust_texts() == 0 and len(store.writes) == writes + 4
    table = pq.ParquetFile(pa.BufferReader(store.objects[key])).read()
    assert table.num_columns == 53
    assert {name for name in table.column_names if name.startswith("description")} == {"description_clean"}
    assert not any(name.startswith("quality_") for name in table.column_names)
    assert trusted.trust_texts() == 0 and len(store.writes) == writes + 4


def test_rejects_missing_columns_and_invalid_threshold():
    with pytest.raises(ValueError):
        assess_and_clean(pa.table({"id": [1]}), "2026-10-20", row_provenance=[])
    with pytest.raises(ValueError):
        trusted.trust_texts(min_description_chars=0)


@pytest.mark.parametrize("damage", ["missing", "reordered", "wrong_id", "missing_field"])
def test_manifest_row_binding_must_match_parquet(store, damage):
    add_partition(store, ids=(1, 2))
    key = ("2.formatted-zone", "texts/rawg/manifests/collection_date=2026-10-20.json")
    checkpoint = json.loads(store.objects[key])
    if damage == "missing":
        checkpoint["row_provenance"].pop()
    elif damage == "reordered":
        checkpoint["row_provenance"].reverse()
    elif damage == "wrong_id":
        checkpoint["row_provenance"][0]["id"] = 999
    else:
        del checkpoint["row_provenance"][0]["source_object_key"]
    store.objects[key] = json.dumps(checkpoint).encode()
    assert trusted.trust_texts() == 1 and not store.writes


def test_global_reports_are_repaired_without_reprocessing_partitions(store):
    add_partition(store)
    add_partition(store, "2026-10-22")
    assert trusted.trust_texts() == 0
    overview_key = ("3.trusted-zone", "texts/rawg/quality/overview.json")
    overview = json.loads(store.objects[overview_key])
    assert overview["capture_count"] == 2 and overview["distinct_valid_game_ids"] == 1
    assert overview["captures_per_game"] == {"1": 2}
    assert len(overview["collection_dates"]) == 2
    assert "clip" in overview["completeness"]
    store.objects[overview_key] = b"corrupted"
    writes = len(store.writes)
    assert trusted.trust_texts() == 0
    assert len(store.writes) == writes + 3
    assert all("collection_date=" not in key for _, key in store.writes[writes:])
    assert json.loads(store.objects[overview_key])["capture_count"] == 2
    writes = len(store.writes)
    assert trusted.trust_texts() == 0 and len(store.writes) == writes


def test_global_summary_is_not_replaced_by_incomplete_analysis(store):
    add_partition(store)
    add_partition(store, "2026-10-22")
    assert trusted.trust_texts() == 0
    key = ("3.trusted-zone", "texts/rawg/quality/overview.json")
    original = store.objects[key]
    del store.objects["2.formatted-zone", "texts/rawg/manifests/collection_date=2026-10-22.json"]
    assert trusted.trust_texts() == 1
    assert store.objects[key] == original

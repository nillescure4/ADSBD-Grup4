import hashlib
import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.app.trusted_details import highlight_description, load_snapshot, names, select_game


def test_highlight_is_exact_escaped_and_preserves_full_text():
    text = 'Accents àè\n\n<script>bad</script> **literal** & more\n\nEnd.'
    start = text.index('<script>')
    end = text.index('\n\nEnd.')
    rendered = highlight_description(text, start, end)
    assert '<strong>&lt;script&gt;bad&lt;/script&gt; **literal** &amp; more</strong>' in rendered
    assert 'Accents àè\n\n' in rendered and '\n\nEnd.' in rendered
    assert '<script>' not in rendered
    # Only the winning occurrence is highlighted, even if the words repeat.
    assert 'same <strong>same</strong>' in highlight_description('same same', 5, 9)


@pytest.mark.parametrize('start,end', [(-1, 2), (0, 100), (2, 1), (0, 0)])
def test_invalid_highlight_offsets_are_rejected(start, end):
    with pytest.raises(ValueError):
        highlight_description('text', start, end)


def snapshot():
    rows = [{"id": 1, "name": "Original name", "description_clean": "Complete\n\nOriginal description.",
             "genres": json.dumps([{"name": "Puzzle"}]), "rating": None},
            {"id": 2, "name": "Another game", "description_clean": "Other description.",
             "genres": "[]", "rating": 0}]
    table = pa.Table.from_pylist(rows).replace_schema_metadata({b"json_encoded_fields": b'["genres"]'})
    buffer = pa.BufferOutputStream()
    pq.write_table(table, buffer)
    body = buffer.getvalue().to_pybytes()
    class Store:
        def get_object(self, Bucket, Key):
            assert Bucket == "trusted" and Key == "historical.parquet"
            return {"Body": io.BytesIO(body)}
    return Store(), hashlib.sha256(body).hexdigest()


def result():
    text = "Complete\n\nOriginal description."
    return {"game_id": 1, "text": "Original description.", "metadata": {
        "trusted_row": 0, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "char_start": len("Complete\n\n"), "char_end": len(text)}}


def test_full_record_and_structures_from_exact_historical_snapshot():
    client, digest = snapshot()
    rows = load_snapshot(client, "trusted", "historical.parquet", digest)
    selected = select_game(rows, result())
    assert selected["name"] == "Original name"
    assert selected["description_clean"] == "Complete\n\nOriginal description."
    assert selected["genres"] == [{"name": "Puzzle"}]
    assert selected["rating"] is None
    assert rows[1]["rating"] == 0


def test_modified_snapshot_is_rejected():
    client, _ = snapshot()
    with pytest.raises(ValueError, match="changed"):
        load_snapshot(client, "trusted", "historical.parquet", "wrong hash")


@pytest.mark.parametrize("problem", ["wrong_row", "missing_row", "negative_row", "changed_text", "changed_chunk"])
def test_stale_provenance_cannot_show_another_game_or_description(problem):
    client, digest = snapshot()
    rows = load_snapshot(client, "trusted", "historical.parquet", digest)
    hit = result()
    if problem == "wrong_row":
        hit["metadata"]["trusted_row"] = 1
    elif problem == "missing_row":
        hit["metadata"]["trusted_row"] = 20
    elif problem == "negative_row":
        hit["metadata"]["trusted_row"] = -1
    elif problem == "changed_text":
        rows[0]["description_clean"] = "Updated version"
    else:
        hit["text"] = "Incorrect passage"
    with pytest.raises(ValueError):
        select_game(rows, hit)


def test_names_supports_platforms_duplicates_and_missing_data():
    assert names([{"platform": {"name": "PC"}}, {"platform": {"name": "PC"}}], nested="platform") == "PC"
    assert names([{"name": "Puzzle"}, {"name": "Action"}]) == "Puzzle, Action"
    assert names([]) == names(None) == names([{"name": 5}]) == "Not available"

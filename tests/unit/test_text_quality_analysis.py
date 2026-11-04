import math

import pyarrow as pa
import pytest

from src.trusted.text_quality_analysis import (
    completeness_profile, numeric_profile, overview_report, review_sample,
    select_review_samples,
)


def test_numeric_distribution_excludes_nulls_and_nonfinite_without_modifying_values():
    values = [None, float("nan"), float("inf"), -float("inf"), 0, 1, 2, 3, 100]
    rows = [{"rating": value, "id": index} for index, value in enumerate(values)]
    profile = numeric_profile(rows, ["rating", "id"])
    rating = profile["rating"]
    assert rating["finite_count"] == 5 and rating["nonfinite_count"] == 3 and rating["null_count"] == 1
    assert rating["min"] == 0 and rating["median"] == 2 and rating["max"] == 100
    assert rating["mean"] == pytest.approx(21.2)
    assert rating["p95"] == pytest.approx(80.6)
    assert rating["q1"] == 1 and rating["q3"] == 3
    assert rating["potential_extremes"]["count"] == 1
    assert sum(bin["count"] for bin in rating["histogram"]) == 5
    assert profile["id"]["potential_extremes"]["count"] is None
    assert math.isnan(rows[1]["rating"]) and rows[-1]["rating"] == 100


def test_null_constant_and_small_numeric_samples():
    empty = numeric_profile([{"a": None}, {"a": None}], ["a"])["a"]
    assert empty["null_pct"] == 100 and empty["median"] is None and empty["histogram"] == []
    constant = numeric_profile([{"a": 7}] * 5, ["a"])["a"]
    assert constant["median"] == 7 and constant["histogram"] == [{"lower": 7, "upper": 7, "count": 5}]
    small = numeric_profile([{"a": 1}, {"a": 999}], ["a"])["a"]
    assert small["potential_extremes"]["count"] is None


def test_completeness_distinguishes_zero_null_blank_and_empty_arrays():
    rows = [{"a": None, "genres": "[]"}, {"a": " ", "genres": None}, {"a": 0, "genres": "[1]"}]
    profile = completeness_profile(rows, ["a", "genres"], {"genres"})
    assert profile["a"]["missing_count"] == 2 and profile["a"]["missing_pct"] == pytest.approx(200 / 3)
    assert profile["genres"]["null_count"] == 1 and profile["genres"]["empty_json_container_count"] == 1
    assert profile["genres"]["missing_count"] == 1


def sample(original, cleaned, game_id=1):
    return review_sample({"description_raw": original, "description_clean": cleaned}, {
        "id": game_id, "collection_date": "2026-10-20", "input_row": 0, "output_row": 0,
        "source_object_key": "source.json", "description_clean_source": "description_raw" if cleaned else None,
        "flags": [],
    })


def test_qualitative_samples_show_changes_and_do_not_claim_semantic_approval():
    record = sample("Paràgraf À.\n\nSegon paràgraf!", "Paràgraf À.\n\nSegon paràgraf!")
    assert record["clean_paragraphs"] == 2 and record["source_paragraphs"] == 2
    assert not record["changed"] and not record["checks"]
    assert record["review_status"] == "pending_human_review"
    large = sample("A" * 300, "A")
    assert large["character_reduction_pct"] > 50 and large["checks"]
    lost = sample("<script>text</script>", None)
    assert "text_became_empty" in lost["checks"] and lost["before_excerpt"]
    truncated = sample("A" * 2500, "A" * 2500)
    assert truncated["before_truncated"] and len(truncated["before_excerpt"]) == 2000
    assert truncated["clean_characters"] == 2500
    chosen = select_review_samples([record, large, sample("Old", "New", 2)], limit=2)
    assert len(chosen) == 2 and len({item["id"] for item in chosen}) == 2
    assert chosen[0]["checks"]


def test_global_report_distinguishes_captures_games_and_recurring_issues():
    partitions = []
    for day in ("2026-10-20", "2026-10-22"):
        table = pa.table({"id": [1, 2], "rating": [3.0, 4.0], "clip": [None, None]})
        report = {
            "collection_date": day, "before": {"rows": 2},
            "after": {"rows_with_warnings": 1, "flag_counts": {"missing_description": 1}},
            "corrections": {"exact_duplicates_removed": 0, "text_changed_rows": 1},
            "qualitative_review": {"samples": []},
        }
        partitions.append((report, table))
    report = overview_report(partitions)
    assert report["capture_count"] == 4 and report["distinct_valid_game_ids"] == 2
    assert report["captures_per_game"] == {"1": 2, "2": 2}
    assert report["warnings"] == 2
    assert report["recurring_flags"]["missing_description"] == {"row_count": 2, "dates_count": 2}
    assert report["completeness"]["clip"]["missing_pct"] == 100
    assert any("clip" in note and "100%" in note for note in report["interpretation"])
    assert "not independent observations" in report["interpretation"][0]
    assert report["numeric_profile"]["rating"]["median"] == 3.5


def test_empty_overview_is_explicit_and_serializable():
    import json
    report = overview_report([])
    assert report["capture_count"] == report["distinct_valid_game_ids"] == 0
    assert report["numeric_profile"] == {} and report["collection_dates"] == []
    json.dumps(report, allow_nan=False)

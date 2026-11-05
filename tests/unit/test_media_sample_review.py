import numpy as np
import pytest

from src.trusted.media_sample_review import difference, select_samples


def test_comparison_requires_identical_shape_and_measures_all_samples():
    before = np.array([0., 1., -1.], dtype=np.float32)
    assert difference(before, before)["identical"]
    changed = difference(before, np.array([0., 1., 0.], dtype=np.float32))
    assert not changed["identical"]
    assert changed["max_absolute_error"] == 1
    assert changed["rms_error"] == pytest.approx(1 / np.sqrt(3))
    with pytest.raises(ValueError, match="different shapes"):
        difference(before, before[:2])


def test_sampling_covers_extremes_without_duplicate_approvals():
    rows = [{"sample_id": str(i), "brightness": i, "sharpness": 10 - i} for i in range(6)]
    selected = select_samples(rows, ["brightness", "sharpness"], 4)
    assert len(selected) == len({r["sample_id"] for r in selected}) == 4
    assert {"0", "5"} <= {r["sample_id"] for r in selected}
    assert select_samples([], ["brightness"], 4) == []

import json
import math
import sys
from pathlib import Path

import pytest

from bistra.durations import environment_key, load_history, save_history, update_history


def test_current_cache_accepts_only_finite_nonnegative_numbers(tmp_path):
    path = tmp_path / "history.json"
    observations = {
        "test_cases.py::test_valid[a]": 2,
        "test_cases.py::test_valid[b]": 0,
        "negative": -1,
        "nan": math.nan,
        "inf": math.inf,
        "bool": True,
        "string": "1.5",
        "null": None,
        "too_large": 10**400,
    }
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "environments": {
                    environment_key(): {nodeid: {"total": value} for nodeid, value in observations.items()}
                },
            }
        )
    )
    assert load_history(path) == {
        "test_cases.py::test_valid[a]": {"total": 2.0},
        "test_cases.py::test_valid[b]": {"total": 0.0},
    }


def test_versioned_cache_retains_valid_phases_and_requires_total(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "environments": {
                    environment_key(): {
                        "valid": {"total": 3, "setup": 1, "call": 2, "teardown": 0},
                        "partial": {"total": 2, "setup": -1, "call": 2, "teardown": True, "unknown": 40},
                        "missing_total": {"call": 2},
                        "invalid_total": {"total": math.inf, "call": 2},
                        "not_record": 2,
                    }
                },
            }
        )
    )
    assert load_history(path) == {
        "valid": {"total": 3, "setup": 1, "call": 2, "teardown": 0},
        "partial": {"total": 2, "call": 2},
    }


@pytest.mark.parametrize(
    "contents",
    [
        "not JSON",
        "[]",
        "null",
        '{"version": 2, "tests": {}}',
        '{"version": true, "tests": {}}',
        '{"version": 1.0, "tests": {}}',
        '{"version": 1, "tests": []}',
        '{"version": 1}',
        '{"version": 2, "environments": []}',
        '{"old_test": 5}',
    ],
)
def test_malformed_and_unknown_schema_is_optional(tmp_path, contents):
    path = tmp_path / "history.json"
    path.write_text(contents)
    assert load_history(path) == {}


def test_missing_unreadable_and_non_utf8_cache_is_optional(tmp_path):
    assert load_history(tmp_path / "missing") == {}
    assert load_history(tmp_path) == {}
    path = tmp_path / "history.json"
    path.write_bytes(b"\xff")
    assert load_history(path) == {}


def test_smoothing_preserves_unmeasured_tests_and_parameterized_identity():
    first = "test_cases.py::test_case[first]"
    second = "test_cases.py::test_case[second]"
    history = {
        first: {"total": 10, "setup": 2, "call": 6, "teardown": 2},
        second: {"total": 3},
    }
    samples = {
        first: {"total": 6, "setup": 0, "call": 6, "teardown": 0},
        "new": {"total": 4, "setup": 1, "call": 3, "teardown": 0},
    }
    updated = update_history(history, samples)
    assert updated == {
        first: {"total": 8, "setup": 1, "call": 6, "teardown": 1},
        second: {"total": 3},
        "new": samples["new"],
    }
    assert history[first]["total"] == 10
    assert samples[first]["total"] == 6
    updated[second]["total"] = 100
    assert history[second]["total"] == 3


def test_partial_samples_acquire_phases_without_smoothing_against_zero():
    assert update_history(
        {"test": {"total": 10}},
        {
            "test": {"total": 6, "setup": 1, "call": 4, "teardown": 1},
        },
    ) == {"test": {"total": 8, "setup": 1, "call": 4, "teardown": 1}}


def test_missing_phase_preserves_previous_estimate_and_invalid_sample_is_ignored():
    history = {"test": {"total": 4, "setup": 1, "call": 3}}
    assert update_history(history, {"test": {"total": 2}}) == {
        "test": {"total": 3, "setup": 1, "call": 3},
    }
    assert update_history(history, {"test": {"total": math.nan}}) == history


def test_smoothing_large_finite_estimates_does_not_overflow():
    assert update_history(
        {"test": {"total": 1e308}},
        {
            "test": {"total": 1e308},
        },
    ) == {"test": {"total": 1e308}}


def test_save_round_trip_creates_parent_and_preserves_schema(tmp_path):
    path = tmp_path / "nested" / "history.json"
    history = {"test": {"total": 3, "setup": 1, "call": 2, "teardown": 0}}
    save_history(path, history)
    assert json.loads(path.read_text()) == {"version": 2, "environments": {environment_key(): history}}
    assert load_history(path) == history
    assert set(path.parent.iterdir()) == {path, path.parent / ".gitignore"}
    assert (path.parent / ".gitignore").read_text() == "*\n"


def test_failed_atomic_replace_preserves_old_cache_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    save_history(path, {"old": {"total": 5}})

    def fail_replace(self, destination):
        raise OSError("replacement blocked")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement blocked"):
        save_history(path, {"new": {"total": 2}})
    assert load_history(path) == {"old": {"total": 5}}
    assert set(tmp_path.iterdir()) == {path, tmp_path / ".gitignore"}


def test_unwritable_cache_parent_raises_to_caller(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("preserve")
    with pytest.raises(OSError):
        save_history(blocked / "history.json", {})
    assert blocked.read_text() == "preserve"


def test_python_environments_keep_separate_history(tmp_path, monkeypatch):
    path = tmp_path / "timings.json"
    original_prefix = sys.prefix
    save_history(path, {"first": {"total": 1}})
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "other-venv"))
    assert load_history(path) == {}
    save_history(path, {"second": {"total": 2}})
    assert load_history(path) == {"second": {"total": 2}}
    monkeypatch.setattr(sys, "prefix", original_prefix)
    assert load_history(path) == {"first": {"total": 1}}


def test_existing_ignore_file_is_preserved(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text("# User comment\n*\n")
    save_history(tmp_path / "timings.json", {})
    assert ignore.read_text() == "# User comment\n*\n"


def test_invalid_records_in_another_environment_do_not_break_saving(tmp_path):
    path = tmp_path / "timings.json"
    path.write_text(json.dumps({"version": 2, "environments": {"other": {"test": {"total": math.nan}}}}))
    save_history(path, {"current": {"total": 1}})
    assert load_history(path) == {"current": {"total": 1}}

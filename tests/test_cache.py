import subprocess
import sys

import pytest

from bistra.durations import history_path, load_history


def _run(root, env, *args, cwd=None, workers=2):
    result = subprocess.run(
        [sys.executable, "-m", "bistra", "-j", str(workers), *args],
        cwd=cwd or root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize("workers", [1, 2])
def test_history_profiles_once_and_uses_project_root(tmp_path, subprocess_env, workers):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_one(): assert 1 + 1 == 2\n")
    cold = _run(tmp_path, subprocess_env, cwd=tests, workers=workers)
    assert f"Running 1 test with {workers} worker" in cold
    assert cold.count("Profiling tests") == 1
    path = history_path(tmp_path)
    assert set(load_history(path)) == {"tests/test_sample.py::test_one"}
    assert not history_path(tests).exists()
    assert (path.parent / ".gitignore").read_text() == "*\n"
    warm = _run(tmp_path, subprocess_env, workers=workers)
    assert "Profiling tests" not in warm
    assert "1 passed" in warm
    (tests / "test_sample.py").write_text("def test_one(): pass\ndef test_two(): pass\n")
    quiet = _run(tmp_path, subprocess_env, "-q", workers=workers)
    assert "2 passed" in quiet
    assert "Running " not in quiet and "Profiling tests" not in quiet
    assert len(load_history(path)) == 2
    path.write_text("broken json")
    assert "Profiling tests" in _run(tmp_path, subprocess_env, workers=workers)
    assert len(load_history(path)) == 2


def test_unwritable_optional_history_does_not_change_test_results(tmp_path, subprocess_env):
    blocked = tmp_path / ".bistra_cache"
    blocked.write_text("preserve")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_sample.py").write_text("def test_one(): assert 1 + 1 == 2\n")
    output = _run(tmp_path, subprocess_env, "-q")
    assert "1 passed" in output
    assert "could not save test timings" in output
    assert blocked.read_text() == "preserve"


def test_collect_only_does_not_profile_or_write_history(tmp_path, subprocess_env):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_sample.py").write_text("def test_one(): pass\n")
    output = _run(tmp_path, subprocess_env, "--collect-only")
    assert "Profiling tests" not in output
    assert not history_path(tmp_path).exists()

import os
import subprocess
import sys


def test_unwritable_optional_cache_does_not_change_test_results(tmp_path):
    blocked = tmp_path / "cache-is-a-file"
    blocked.write_text("preserve")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_sample.py").write_text("def test_one(): assert 1 + 1 == 2\n")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
           "XDG_CACHE_HOME": str(blocked)}
    result = subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "2", "--", "-q"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "could not save optional duration cache" in result.stdout
    assert blocked.read_text() == "preserve"

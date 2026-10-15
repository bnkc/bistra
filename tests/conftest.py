import os

import pytest

pytest_plugins = "pytester"


@pytest.fixture
def subprocess_env(tmp_path):
    env = os.environ.copy()
    # Nested pytest runs need their own identity when this suite runs under Bistra.
    for variable in ("PYTEST_PLUGINS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        env.pop(variable, None)
    env["PYTEST_ADDOPTS"] = ""
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    return env

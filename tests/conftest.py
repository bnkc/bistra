import os

import pytest

pytest_plugins = "pytester"


@pytest.fixture
def subprocess_env(tmp_path):
    env = os.environ.copy()
    env.pop("PYTEST_PLUGINS", None)
    env["PYTEST_ADDOPTS"] = ""
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    return env

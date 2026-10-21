import pytest

pytest_plugins = ("pytester",)


@pytest.fixture
def pytester(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    for name in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    pytester.makeini("[pytest]")
    return pytester

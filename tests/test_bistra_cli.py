from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import version

import pytest
from bistra.cli import main


def test_cli_forwards_pytest_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(pytest, "main", lambda args, **kwargs: calls.append(args) or 5)
    assert (
        main(
            [
                "-j",
                "2",
                "-q",
                "--",
                "tests/",
                "-k",
                "selected",
            ]
        )
        == 5
    )
    assert calls == [
        [
            "--bistra",
            "--bistra-workers",
            "2",
            "-q",
            "tests/",
            "-k",
            "selected",
        ]
    ]


@pytest.mark.parametrize("arguments", [["-j", "0"], ["--workers", "-1"]])
def test_cli_rejects_invalid_counts(arguments):
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_cli_version(capsys):
    from bistra import __version__

    installed_version = version("bistra")
    assert __version__ == installed_version
    with pytest.raises(SystemExit) as error:
        main(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out == f"bistra {installed_version}\n"


def test_installed_plugin_adds_help_options(pytester, monkeypatch):
    for variable in ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        monkeypatch.delenv(variable, raising=False)

    result = pytester.runpytest_subprocess("--help")

    assert result.ret == 0
    result.stdout.fnmatch_lines(["*--bistra *", "*--bistra-workers=*"])


def _run(tmp_path, arguments, *, autoload=False, expected_workers=1, enabled=False):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "conftest.py").write_text("""
import json
import os
from pathlib import Path

assert ("PYTEST_XDIST_WORKER" in os.environ) == (int(os.environ["BISTRA_EXPECT_WORKERS"]) > 1)

def pytest_collection_modifyitems(session):
    destination = Path(f"collection-{os.getpid()}.json")
    assert session.config.pluginmanager.hasplugin("bistra-engine") == bool(int(os.environ["BISTRA_EXPECT_ENABLED"]))
    assert not destination.exists(), "collection repeated in the same process"
    destination.write_text(json.dumps({"pid": os.getpid(), "items": len(session.items)}))
""")
    (tmp_path / "test_example.py").write_text("""
import json
import os
from pathlib import Path
import pytest

@pytest.fixture
def value():
    return 42

@pytest.mark.parametrize("index", range(4))
def test_execution(index, value, request):
    assert value == 42
    collection = json.loads(Path(f"collection-{os.getpid()}.json").read_text())
    if int(os.environ["BISTRA_EXPECT_WORKERS"]) > 1:
        assert os.getpid() == collection["pid"]
        assert request.config.workerinput["workerid"].startswith("bistra")
    else:
        assert os.getpid() == collection["pid"]
    import time
    time.sleep(0.02)
    Path(f"executed-{index}.json").write_text(json.dumps({"pid": os.getpid()}))
""")
    env = os.environ.copy()
    env.pop("PYTEST_PLUGINS", None)
    if autoload:
        env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    else:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTEST_ADDOPTS"] = ""
    env["BISTRA_EXPECT_WORKERS"] = str(expected_workers)
    env["BISTRA_EXPECT_ENABLED"] = str(int(enabled))
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    process = subprocess.run(
        [sys.executable, "-m", *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "4 passed" in process.stdout
    assert "PytestAssertRewriteWarning" not in process.stdout + process.stderr
    assert len(list(tmp_path.glob("collection-*.json"))) == expected_workers
    assert all(
        json.loads(path.read_text())["items"] == 4
        for path in tmp_path.glob("collection-*.json")
    )
    records = [
        json.loads((tmp_path / f"executed-{i}.json").read_text()) for i in range(4)
    ]
    assert len({record["pid"] for record in records}) == expected_workers
    return process


def test_installation_does_not_enable_runner(tmp_path):
    process = _run(tmp_path, ["pytest", "-q"], autoload=True)
    assert "bistra: dispatching" not in process.stdout


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork workers require POSIX")
@pytest.mark.parametrize("autoload", [False, True])
def test_cli_loads_plugin_once_with_or_without_autoload(tmp_path, autoload):
    _run(
        tmp_path,
        ["bistra", "-j", "2", "--", "-q", "-Werror"],
        autoload=autoload,
        expected_workers=2,
        enabled=True,
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork workers require POSIX")
def test_pytest_opt_in_entry_point(tmp_path):
    _run(
        tmp_path,
        ["pytest", "--bistra", "--bistra-workers", "2", "-q"],
        autoload=True,
        expected_workers=2,
        enabled=True,
    )


def test_cli_one_worker_uses_pytest_serial_execution(tmp_path):
    _run(tmp_path, ["bistra", "-j", "1", "--", "-q"], enabled=True)


def test_explicit_plugin_loading_stays_disabled_without_opt_in(tmp_path):
    _run(tmp_path, ["pytest", "-p", "bistra", "-q"])


def test_pytest_rejects_invalid_worker_count(tmp_path):
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTEST_ADDOPTS"] = ""
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "bistra",
            "--bistra",
            "--bistra-workers",
            "0",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 4
    assert "must be a positive integer" in process.stderr

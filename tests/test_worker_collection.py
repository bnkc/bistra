"""Worker collection supplies controller reporting without importing the suite twice."""

import json
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pytest


def run(tmp_path, subprocess_env, *args):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    return subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "2", "--", "-q", *args],
        cwd=tmp_path,
        env=subprocess_env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_controller_reports_without_importing_collection_only_modules(tmp_path, subprocess_env):
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        from pathlib import Path

        def pytest_sessionstart(session):
            if hasattr(session.config, "workerinput"):
                assert Path("shared-resource").exists()
            else:
                Path("shared-resource").touch()

        def pytest_collection_modifyitems(config):
            assert hasattr(config, "workerinput"), "controller must not collect"

        def pytest_sessionfinish(session):
            if hasattr(session.config, "workerinput"):
                assert Path("shared-resource").exists()
                Path("finished-" + session.config.workerinput["workerid"]).touch()
            else:
                assert len(list(Path().glob("finished-*"))) == 2
                Path("controller.json").write_text(json.dumps({
                    "items": len(session.items), "collected": session.testscollected,
                }))
                Path("shared-resource").unlink()
    """)
    )
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "conftest.py").write_text(
        textwrap.dedent("""
        import os
        from pathlib import Path
        import pytest

        assert "PYTEST_XDIST_WORKER" in os.environ, "controller imported nested conftest"
        Path("nested-" + os.environ["PYTEST_XDIST_WORKER"]).touch()

        @pytest.fixture
        def resource():
            assert Path("shared-resource").exists()
            yield
            assert Path("shared-resource").exists()
    """)
    )
    (suite / "test_cases.py").write_text(
        textwrap.dedent("""
        import os
        import pytest

        assert "PYTEST_XDIST_WORKER" in os.environ, "controller imported test module"

        @pytest.mark.parametrize("index", range(3))
        def test_pass(index, resource):
            pass

        @pytest.mark.skip(reason="selection coverage")
        def test_skip():
            pass

        @pytest.mark.xfail(reason="report coverage", strict=True)
        def test_expected_failure():
            assert False
    """)
    )
    result = run(tmp_path, subprocess_env, "--junitxml=report.xml")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed, 1 skipped, 1 xfailed" in result.stdout
    assert json.loads((tmp_path / "controller.json").read_text()) == {"items": 0, "collected": 5}
    assert not (tmp_path / "shared-resource").exists()
    assert sorted(path.name for path in tmp_path.glob("nested-*")) == ["nested-bistra0", "nested-bistra1"]
    report = ET.parse(tmp_path / "report.xml").getroot().find("testsuite")
    assert report is not None
    assert {key: report.attrib[key] for key in ("tests", "errors", "failures", "skipped")} == {
        "tests": "5",
        "errors": "0",
        "failures": "0",
        "skipped": "2",
    }
    assert len(report.findall("testcase")) == 5


@pytest.mark.parametrize("selection,passed,deselected", [("chosen", 2, 2), ("missing", 0, 4)])
def test_worker_deselection_and_collection_warning_reach_controller(
    tmp_path, subprocess_env, selection, passed, deselected
):
    (tmp_path / "test_selection.py").write_text(
        textwrap.dedent("""
        import warnings
        import pytest

        warnings.warn("worker collection warning", UserWarning)

        @pytest.mark.parametrize("index", range(2))
        def test_chosen(index):
            pass

        @pytest.mark.parametrize("index", range(2))
        def test_ignored(index):
            raise AssertionError("deselected tests must not execute")
    """)
    )
    result = run(tmp_path, subprocess_env, "-k", selection)
    output = result.stdout + result.stderr
    assert result.returncode == (0 if passed else 5), output
    if passed:
        assert f"{passed} passed" in output
    assert f"{deselected} deselected" in output
    assert "UserWarning: worker collection warning" in output


def test_collection_skip_is_reported_with_no_selected_tests(tmp_path, subprocess_env):
    (tmp_path / "test_skipped.py").write_text(
        'import pytest\npytest.skip("optional dependency unavailable", allow_module_level=True)\n'
    )
    result = run(tmp_path, subprocess_env, "-rs", "--junitxml=report.xml")
    output = result.stdout + result.stderr
    assert result.returncode == 5, output
    assert "1 skipped" in output
    assert "optional dependency unavailable" in output
    report = ET.parse(tmp_path / "report.xml").getroot().find("testsuite")
    assert report is not None
    assert report.attrib["skipped"] == "1"


@pytest.mark.parametrize("category", ["UserWarning", "CollectionWarning"])
def test_warning_from_other_worker_reaches_controller_without_importing_test_module(tmp_path, subprocess_env, category):
    (tmp_path / "test_warning.py").write_text(
        textwrap.dedent(f"""
        import os
        from pathlib import Path
        import warnings

        if "PYTEST_XDIST_WORKER" not in os.environ:
            Path("controller-imported-test-module").touch()
            raise AssertionError("controller must not import test module to replay a warning")

        class CollectionWarning(UserWarning):
            pass

        if os.environ["PYTEST_XDIST_WORKER"] == "bistra1":
            warnings.warn("other worker collection evidence", {category})

        def test_ok():
            pass
    """)
    )
    result = run(tmp_path, subprocess_env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output
    assert f"{category}: other worker collection evidence" in output
    assert not (tmp_path / "controller-imported-test-module").exists()


def test_collection_failure_reaches_junit_before_workers_are_cleaned_up(tmp_path, subprocess_env):
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        from pathlib import Path

        def pytest_sessionfinish(session):
            if hasattr(session.config, "workerinput"):
                session.config.workeroutput["finished"] = session.config.workerinput["workerid"]

        def pytest_testnodedown(node, error):
            Path("node-output-" + node.gateway.id + ".json").write_text(json.dumps(node.workeroutput))
    """)
    )
    (tmp_path / "test_broken.py").write_text('raise ImportError("missing collection dependency")\n')
    (tmp_path / "test_unused.py").write_text(
        'from pathlib import Path\ndef test_unused():\n    Path("unexpected-test").touch()\n'
    )
    result = run(tmp_path, subprocess_env, "--junitxml=report.xml")
    assert result.returncode == 4, result.stdout + result.stderr
    assert not (tmp_path / "unexpected-test").exists()
    report = ET.parse(tmp_path / "report.xml").getroot().find("testsuite")
    assert report is not None
    assert report.attrib["errors"] == "1"
    assert "missing collection dependency" in ET.tostring(report, encoding="unicode")
    assert {
        path.stem.removeprefix("node-output-"): json.loads(path.read_text())
        for path in tmp_path.glob("node-output-*.json")
    } == {worker: {"finished": worker} for worker in ("bistra0", "bistra1")}

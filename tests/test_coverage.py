import json
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pytest


@pytest.fixture
def covered_project(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "sample.py").write_text(
        "def choose(value):\n"
        "    if value:\n"
        "        return 'yes'\n"
        "    return 'no'\n"
        "\n"
        "def never_called():\n"
        "    return 'missing'\n"
    )
    for name, value, expected in (("yes", True, "yes"), ("no", False, "no")):
        (tmp_path / f"test_{name}.py").write_text(
            textwrap.dedent(f"""
            import os
            from pathlib import Path
            from sample import choose

            def test_{name}():
                Path("{name}.pid").write_text(str(os.getpid()))
                assert choose({value}) == "{expected}"
            """)
        )
    return tmp_path


def run_coverage(root, env, *args, workers=2):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bistra",
            "-j",
            str(workers),
            "-p",
            "pytest_cov.plugin",
            "--cov=sample",
            "--cov-branch",
            "--cov-report=json:coverage.json",
            "-q",
            *args,
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def coverage_file(root):
    report = json.loads((root / "coverage.json").read_text())
    assert set(report["files"]) == {"sample.py"}
    return report["files"]["sample.py"]


@pytest.mark.parametrize("workers", [1, 2])
def test_combines_branches_contexts_and_report_formats(covered_project, subprocess_env, workers):
    root = covered_project
    result = run_coverage(
        root,
        subprocess_env,
        "--cov-context=test",
        "--cov-report=xml:coverage.xml",
        "--cov-report=term-missing",
        workers=workers,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert "sample.py" in result.stdout
    assert len({(root / f"{name}.pid").read_text() for name in ("yes", "no")}) == workers
    data = coverage_file(root)
    assert data["executed_lines"] == [1, 2, 3, 4, 6]
    assert data["missing_lines"] == [7]
    assert data["executed_branches"] == [[2, 3], [2, 4]]
    assert data["missing_branches"] == []
    tree = ET.parse(root / "coverage.xml")
    lines = {line.attrib["number"]: line.attrib for line in tree.findall(".//class/lines/line")}
    assert lines["3"]["hits"] == lines["4"]["hits"] == "1"
    assert lines["7"]["hits"] == "0"
    assert lines["2"]["condition-coverage"] == "100% (2/2)"
    contexts = subprocess.run(
        [
            sys.executable,
            "-c",
            "import coverage, json; d = coverage.CoverageData(); d.read(); "
            "print(json.dumps(sorted(d.measured_contexts())))",
        ],
        cwd=root,
        env=subprocess_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert set(json.loads(contexts.stdout)) == {"", "test_yes.py::test_yes|run", "test_no.py::test_no|run"}


def test_fail_under_uses_combined_worker_data(covered_project, subprocess_env):
    root = covered_project
    result = run_coverage(root, subprocess_env, "--cov-fail-under=90")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert "Required test coverage of 90%" in result.stdout
    assert coverage_file(root)["summary"]["percent_covered"] == 87.5
    result = run_coverage(root, subprocess_env, "--cov-fail-under=85")
    assert result.returncode == 0, result.stdout + result.stderr
    assert coverage_file(root)["summary"]["percent_covered"] == 87.5


def test_append_preserves_previous_invocation_data(covered_project, subprocess_env):
    root = covered_project
    first = run_coverage(root, subprocess_env, "test_yes.py")
    assert first.returncode == 0, first.stdout + first.stderr
    assert coverage_file(root)["missing_lines"] == [4, 7]
    second = run_coverage(root, subprocess_env, "test_no.py", "--cov-append")
    assert second.returncode == 0, second.stdout + second.stderr
    assert coverage_file(root)["missing_lines"] == [7]
    assert coverage_file(root)["executed_branches"] == [[2, 3], [2, 4]]


def test_failing_test_still_contributes_coverage(covered_project, subprocess_env):
    root = covered_project
    path = root / "test_yes.py"
    path.write_text(path.read_text().replace('== "yes"', '== "wrong"'))
    result = run_coverage(root, subprocess_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed, 1 passed" in result.stdout
    assert coverage_file(root)["executed_branches"] == [[2, 3], [2, 4]]


def test_worker_conftest_execution_is_measured(covered_project, subprocess_env):
    root = covered_project
    (root / "test_yes.py").unlink()
    (root / "conftest.py").write_text(
        textwrap.dedent("""
        import os

        if os.environ.get("PYTEST_XDIST_WORKER"):
            from sample import choose
            assert choose(True) == "yes"
        """)
    )
    result = run_coverage(root, subprocess_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert coverage_file(root)["executed_branches"] == [[2, 3], [2, 4]]
    assert coverage_file(root)["missing_lines"] == [7]


def test_embedded_parallel_then_serial_run_restores_coverage_plugin(covered_project, subprocess_env):
    root = covered_project
    (root / "embedded.py").write_text(
        textwrap.dedent("""
        import pytest
        import pytest_cov.plugin

        if __name__ == "__main__":
            original = pytest_cov.plugin.CovPlugin
            args = ["-p", "bistra.plugin", "-p", "pytest_cov.plugin",
                    "--cov=sample", "--cov-branch", "-q"]
            assert pytest.main([*args, "--bistra", "--bistra-workers=2",
                                "--cov-report=json:coverage.json"]) == 0
            assert pytest_cov.plugin.CovPlugin is original
            assert pytest.main([*args, "--cov-report=json:serial.json"]) == 0
            assert pytest_cov.plugin.CovPlugin is original
        """)
    )
    result = subprocess.run(
        [sys.executable, "embedded.py"],
        cwd=root,
        env=subprocess_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    parallel = coverage_file(root)
    serial = json.loads((root / "serial.json").read_text())["files"]["sample.py"]
    assert parallel["executed_lines"] == serial["executed_lines"] == [1, 2, 3, 4, 6]
    assert parallel["executed_branches"] == serial["executed_branches"] == [[2, 3], [2, 4]]


def test_nested_serial_coverage_during_conftest_import(covered_project, subprocess_env):
    root = covered_project
    nested = root / "nested"
    nested.mkdir()
    (nested / "test_nested.py").write_text(
        "from sample import choose\n\ndef test_nested():\n    assert choose(True) == 'yes'\n"
    )
    (root / "nested.coveragerc").write_text("[run]\ndata_file = nested-coverage.sqlite\n[json]\nshow_contexts = true\n")
    (root / "conftest.py").write_text(
        textwrap.dedent("""
        import os
        import pytest

        if not os.environ.get("PYTEST_XDIST_WORKER"):
            assert pytest.main([
                "-p", "bistra.plugin", "-p", "pytest_cov.plugin",
                "--confcutdir=nested", "--cov=sample", "--cov-context=test",
                "--cov-config=nested.coveragerc", "--cov-report=json:nested.json",
                "-q", "nested",
            ]) == 0
        """)
    )
    result = run_coverage(root, subprocess_env, "test_yes.py", "test_no.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    nested_data = json.loads((root / "nested.json").read_text())["files"]["sample.py"]
    assert "nested/test_nested.py::test_nested|run" in nested_data["contexts"]["3"]
    assert coverage_file(root)["executed_branches"] == [[2, 3], [2, 4]]


def test_no_cov_disables_measurement_and_reports(covered_project, subprocess_env):
    root = covered_project
    result = run_coverage(root, subprocess_env, "--no-cov", "--cov-fail-under=100")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert not (root / ".coverage").exists()
    assert not (root / "coverage.json").exists()


def test_collect_only_does_not_measure_test_bodies(covered_project, subprocess_env):
    root = covered_project
    result = run_coverage(root, subprocess_env, "--collect-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 tests collected" in result.stdout
    assert not (root / "yes.pid").exists()
    assert not (root / "no.pid").exists()
    assert coverage_file(root)["executed_lines"] == [1, 6]
    assert coverage_file(root)["executed_branches"] == []

import json
import sys

import pytest


def test_coverage_combines_workers_and_includes_conftest_imports(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        sample="""
            def startup():
                return "ready"

            def choose(value):
                if value:
                    return "yes"
                return "no"
        """,
        test_sample="""
            import os
            from pathlib import Path
            import pytest
            from sample import choose

            @pytest.mark.parametrize("value, expected", [(True, "yes"), (False, "no")])
            def test_choose(value, expected):
                Path(f"{value}.pid").write_text(str(os.getpid()))
                assert choose(value) == expected
        """,
    )
    pytester.makeconftest(
        """
        import os
        from sample import startup

        if os.environ.get("PYTEST_XDIST_WORKER"):
            assert startup() == "ready"
        """
    )

    result = pytester.run(
        sys.executable,
        "-m",
        "bistra",
        "-j",
        "2",
        "-p",
        "pytest_cov.plugin",
        "--cov=sample",
        "--cov-branch",
        "--cov-report=json:coverage.json",
        timeout=30,
    )

    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=2)
    assert len({path.read_text() for path in pytester.path.glob("*.pid")}) == 2
    report = json.loads((pytester.path / "coverage.json").read_text())["files"]["sample.py"]
    assert report["missing_lines"] == []
    assert report["missing_branches"] == []


@pytest.mark.parametrize(("threshold", "exit_code"), [(50, pytest.ExitCode.OK), (100, pytest.ExitCode.TESTS_FAILED)])
def test_coverage_threshold_controls_exit_status(
    pytester: pytest.Pytester, threshold: int, exit_code: pytest.ExitCode
) -> None:
    pytester.makepyfile(
        sample="""
            def used():
                return 1

            def unused():
                return 2
        """,
        test_sample="from sample import used\ndef test_used(): assert used() == 1\n",
    )

    result = pytester.run(
        sys.executable,
        "-m",
        "bistra",
        "-j",
        "2",
        "-p",
        "pytest_cov.plugin",
        "--cov=sample",
        f"--cov-fail-under={threshold}",
        timeout=30,
    )

    assert result.ret == exit_code
    result.assert_outcomes(passed=1)
    if exit_code == pytest.ExitCode.TESTS_FAILED:
        result.stdout.fnmatch_lines(["*FAIL*coverage*100*"])
    else:
        assert "FAIL" not in result.stdout.str()


def test_nested_serial_coverage_during_conftest_import_is_independent(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        sample="""
            def choose(inner):
                if inner:
                    return "inner"
                return "outer"
        """,
        test_outer="from sample import choose\ndef test_outer(): assert choose(False) == 'outer'\n",
    )
    nested = pytester.mkdir("nested")
    (nested / "test_inner.py").write_text(
        "from sample import choose\ndef test_inner(): assert choose(True) == 'inner'\n"
    )
    (pytester.path / "nested.coveragerc").write_text(
        "[run]\ndata_file = nested-coverage.sqlite\n[json]\nshow_contexts = true\n"
    )
    pytester.makeconftest(
        """
        import os
        import pytest

        if not os.environ.get("PYTEST_XDIST_WORKER"):
            assert pytest.main([
                "-p", "bistra", "-p", "pytest_cov.plugin", "--confcutdir=nested",
                "--cov=sample", "--cov-context=test", "--cov-config=nested.coveragerc",
                "--cov-report=json:nested.json", "nested", "-q",
            ]) == pytest.ExitCode.OK
        """
    )

    result = pytester.run(
        sys.executable,
        "-m",
        "bistra",
        "-j",
        "2",
        "-p",
        "pytest_cov.plugin",
        "--cov=sample",
        "--cov-report=json:coverage.json",
        "test_outer.py",
        timeout=30,
    )

    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)
    inner = json.loads((pytester.path / "nested.json").read_text())["files"]["sample.py"]
    assert inner["executed_lines"] == [1, 2, 3]
    assert inner["contexts"]["3"] == ["nested/test_inner.py::test_inner|run"]
    outer = json.loads((pytester.path / "coverage.json").read_text())["files"]["sample.py"]
    assert outer["executed_lines"] == [1, 2, 4]


@pytest.mark.parametrize(("failure", "skip_report"), [("test", False), ("session", False), ("session", True)])
def test_failure_preserves_coverage_reporting(pytester: pytest.Pytester, failure: str, skip_report: bool) -> None:
    pytester.makepyfile(
        sample="def value():\n    return 42\n",
        test_sample=f"from sample import value\ndef test_value(): assert value() == {0 if failure == 'test' else 42}\n",
    )
    if failure == "session":
        pytester.makeconftest(
            """
            def pytest_sessionfinish(session):
                if hasattr(session.config, "workerinput"):
                    session.exitstatus = 1
            """
        )

    result = pytester.run(
        sys.executable,
        "-m",
        "bistra",
        "-j",
        "2",
        "-p",
        "pytest_cov.plugin",
        "--cov=sample",
        "--cov-report=json:coverage.json",
        *(["--no-cov-on-fail"] if skip_report else []),
        timeout=30,
    )

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(**{"failed" if failure == "test" else "passed": 1})
    report = pytester.path / "coverage.json"
    assert report.exists() == (not skip_report)
    if report.exists():
        assert json.loads(report.read_text())["files"]["sample.py"]["executed_lines"] == [1, 2]

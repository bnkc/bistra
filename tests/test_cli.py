import sys
from importlib.metadata import version
from pathlib import Path

import pytest


def test_help(pytester: pytest.Pytester) -> None:
    result = pytester.run(sys.executable, "-m", "bistra", "--help", timeout=30)

    assert result.ret == 0
    assert result.stderr.lines == []
    assert result.stdout.str() == (
        "Run your pytest suite with automatic parallel scheduling.\n\n"
        "Usage: bistra [OPTIONS] [TESTS] [PYTEST_OPTIONS]\n\n"
        "Options:\n"
        "  -j, --workers <COUNT>  Number of workers [default: auto]\n"
        "  -h, --help             Show help\n"
        "  -V, --version          Show version\n\n"
        "Use `bistra -- --help` for all pytest options."
    )


def test_installed_console_script(pytester: pytest.Pytester) -> None:
    result = pytester.run(Path(sys.executable).with_name("bistra"), "--version", timeout=30)

    assert result.ret == 0
    assert result.stdout.lines == [f"bistra {version('bistra')}"]
    assert result.stderr.lines == []


@pytest.mark.parametrize("count", ["0", "-1", "many"])
def test_invalid_worker_count(pytester: pytest.Pytester, count: str) -> None:
    result = pytester.run(sys.executable, "-m", "bistra", "--workers", count, timeout=30)

    assert result.ret == 2
    result.stderr.fnmatch_lines(["*must be a positive integer or 'auto'*"])
    assert not (pytester.path / ".bistra_cache").exists()


def test_forwards_paths_selection_and_plugin_options(pytester: pytest.Pytester) -> None:
    pytester.makeconftest("""
        def pytest_addoption(parser):
            parser.addoption("--project-token")
    """)
    pytester.makepyfile(
        test_selected="""
            def test_chosen(pytestconfig):
                assert pytestconfig.getoption("project_token") == "forwarded"

            def test_other():
                raise AssertionError("deselected test ran")
        """,
        test_unselected="""
            def test_chosen():
                raise AssertionError("unselected file ran")
        """,
    )

    result = pytester.run(
        sys.executable,
        "-m",
        "bistra",
        "-j",
        "2",
        "test_selected.py",
        "-k",
        "chosen",
        "--",
        "--project-token=forwarded",
        "-q",
        timeout=30,
    )

    assert result.ret == 0
    result.assert_outcomes(passed=1)


def test_separator_exposes_pytest_help(pytester: pytest.Pytester) -> None:
    result = pytester.run(sys.executable, "-m", "bistra", "--", "--help", timeout=30)

    assert result.ret == 0
    result.stdout.fnmatch_lines(["*--fixtures*", "*--collect-only*"])
    assert "Run your pytest suite with automatic parallel scheduling." not in result.stdout.str()


def test_collect_only_does_not_start_workers_or_execute_tests(pytester: pytest.Pytester) -> None:
    pytester.makeconftest("""
        def pytest_configure_node(node):
            raise AssertionError("collect-only started a worker")
    """)
    pytester.makepyfile(
        test_collected="""
        from pathlib import Path

        def test_body():
            Path("executed").touch()
    """
    )

    result = pytester.run(
        sys.executable,
        "-m",
        "bistra",
        "-j",
        "auto",
        "--collect-only",
        "-q",
        timeout=30,
    )

    assert result.ret == 0
    result.stdout.fnmatch_lines(["test_collected.py::test_body"])
    assert not (pytester.path / "executed").exists()
    assert not (pytester.path / ".bistra_cache").exists()


def test_empty_selection_returns_pytests_no_tests_exit_code(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("""
        def test_unselected():
            raise AssertionError("deselected test ran")
    """)

    result = pytester.run(
        sys.executable,
        "-m",
        "bistra",
        "-j",
        "2",
        "-k",
        "nonexistent",
        "-q",
        timeout=30,
    )

    assert result.ret == pytest.ExitCode.NO_TESTS_COLLECTED
    assert not (pytester.path / ".bistra_cache").exists()

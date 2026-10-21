import json
import sys

import pytest

from bistra import timings


def test_serial_cold_and_warm_runs_preserve_outcomes_and_execute_each_body_once(pytester: pytest.Pytester) -> None:
    pytester.makeconftest("""
        import json
        from pathlib import Path
        import pytest

        reports = []

        def pytest_runtest_logreport(report):
            reports.append((report.nodeid, report.when, report.outcome, hasattr(report, "wasxfail")))

        def pytest_sessionfinish(session):
            if not hasattr(session.config, "workerinput"):
                Path("reports.json").write_text(json.dumps(sorted(reports)))

        @pytest.fixture
        def record(request):
            def record_body():
                with (Path("executed") / request.node.name).open("x"):
                    pass
            return record_body

        @pytest.fixture
        def broken_setup():
            pytest.fail("setup failed")

        @pytest.fixture
        def broken_teardown():
            yield
            pytest.fail("teardown failed")
    """)
    pytester.makepyfile(
        test_outcomes="""
        import pytest

        @pytest.mark.parametrize("value", [0, 1])
        def test_pass(value, record):
            record()
            assert value >= 0

        def test_failure(record):
            record()
            pytest.fail("assertion failed")

        @pytest.mark.skip(reason="not available")
        def test_skip(record):
            record()
            raise AssertionError("skipped test ran")

        @pytest.mark.xfail(strict=True, reason="known defect")
        def test_xfail(record):
            record()
            pytest.fail("known defect")

        def test_setup_error(broken_setup, record):
            record()
            raise AssertionError("body ran after failed setup")

        def test_teardown_error(broken_teardown, record):
            record()

        def test_unselected(record):
            record()
            raise AssertionError("deselected test ran")
    """
    )
    executed = pytester.path / "executed"
    executed.mkdir()
    baseline = None
    invocations = [
        ("pytest",),
        ("bistra", "-j", "2"),
        ("bistra", "-j", "2"),
        ("bistra", "-j", "1"),
    ]
    for invocation in invocations:
        (pytester.path / "reports.json").unlink(missing_ok=True)
        result = pytester.run(
            sys.executable,
            "-m",
            *invocation,
            "-k",
            "not unselected",
            "-q",
            timeout=30,
        )

        assert result.ret == pytest.ExitCode.TESTS_FAILED
        result.assert_outcomes(passed=3, failed=1, skipped=1, xfailed=1, errors=2)
        reports = json.loads((pytester.path / "reports.json").read_text())
        if baseline is None:
            baseline = reports
        else:
            assert (pytester.path / ".bistra_cache" / "timings.json").is_file()
        assert reports == baseline
        assert {path.name for path in executed.iterdir()} == {
            "test_pass[0]",
            "test_pass[1]",
            "test_failure",
            "test_xfail",
            "test_teardown_error",
        }
        for path in executed.iterdir():
            path.unlink()


def test_warm_scheduling_balances_work_without_splitting_groups_or_reordering_tests(pytester: pytest.Pytester) -> None:
    pytester.makeconftest("""
        import json
        import os
        from pathlib import Path

        order = {}

        def pytest_collection_modifyitems(items):
            order.update((item.nodeid, index) for index, item in enumerate(items))

        def pytest_runtest_call(item):
            worker = os.environ["PYTEST_XDIST_WORKER"]
            with Path(f"{worker}.jsonl").open("a") as output:
                output.write(json.dumps([order[item.nodeid], item.nodeid]) + "\\n")
    """)
    pytester.makepyfile(
        test_first="""
            import pytest

            @pytest.mark.xdist_group("shared")
            def test_grouped_first():
                pass

            @pytest.mark.parametrize("value", range(4))
            def test_independent(value):
                pass
        """,
        test_last="""
            import pytest

            @pytest.mark.xdist_group("shared")
            def test_grouped_last():
                pass
        """,
    )

    nodeids = [
        "test_first.py::test_grouped_first",
        *(f"test_first.py::test_independent[{value}]" for value in range(4)),
        "test_last.py::test_grouped_last",
    ]
    for warm in (False, True):
        if warm:
            (pytester.path / ".bistra_cache" / "timings.json").unlink()
            timings.save(
                pytester.path,
                {nodeid: timings.Timing(seconds) for nodeid, seconds in zip(nodeids, [1, 9, 9, 1, 1, 1], strict=True)},
            )
        result = pytester.run(sys.executable, "-m", "bistra", "-j", "2", "-q", timeout=30)

        assert result.ret == 0
        result.assert_outcomes(passed=6)
        logs = list(pytester.path.glob("bistra*.jsonl"))
        assert len(logs) == 2
        recorded = []
        owners = {}
        for path in logs:
            records = [json.loads(line) for line in path.read_text().splitlines()]
            indices = [index for index, _ in records]
            assert indices == sorted(indices)
            recorded.extend(nodeid for _, nodeid in records)
            owners.update((nodeid, path.name) for _, nodeid in records)
            path.unlink()
        assert sorted(recorded) == sorted(nodeids)
        assert owners[nodeids[0]] == owners[nodeids[-1]]
        assert (owners[nodeids[1]] != owners[nodeids[2]]) == warm


def test_collection_disagreement_fails_before_executing_tests(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_different="""
        import os
        from pathlib import Path
        import pytest

        count = 1 if os.environ["PYTEST_XDIST_WORKER"] == "bistra0" else 2

        @pytest.mark.parametrize("value", range(count))
        def test_value(value):
            Path("executed").touch()
    """
    )

    result = pytester.run(sys.executable, "-m", "bistra", "-j", "2", "-q", timeout=30)

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*collection differs in tests or test order*", "*test_different.py::test_value[[]1]*"])
    assert not (pytester.path / "executed").exists()


def test_collection_errors_and_warnings_are_not_multiplied_by_workers(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_broken="""
        import os
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("always")
            for _ in range(2):
                warnings.warn("repeated collection warning", UserWarning)
            warnings.warn(f"warning from {os.environ['PYTEST_XDIST_WORKER']}", UserWarning)
        raise RuntimeError("cannot collect this module")
    """
    )
    pytester.makepyfile(
        test_valid="""
        from pathlib import Path

        def test_body():
            Path("executed").touch()
    """
    )

    result = pytester.run(sys.executable, "-m", "bistra", "-j", "2", "-q", timeout=30)

    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.assert_outcomes(errors=1, warnings=4)
    result.stdout.fnmatch_lines(["*RuntimeError: cannot collect this module*"])
    for worker in ("bistra0", "bistra1"):
        assert f"UserWarning: warning from {worker}" in str(result.stdout)
    assert not (pytester.path / "executed").exists()


def test_failure_reports_preserve_capture_and_worker_only_warnings(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        application="""
        import os

        if "PYTEST_XDIST_WORKER" not in os.environ:
            raise RuntimeError("application imported into controller")

        class ApplicationWarning(UserWarning):
            def __init__(self, *, detail):
                super().__init__(detail)
    """
    )
    pytester.makepyfile(
        test_output="""
        import sys
        import warnings
        from application import ApplicationWarning

        def test_failure():
            print("output from test body")
            print("diagnostic from test body", file=sys.stderr)
            warnings.warn(ApplicationWarning(detail="application warning survived"))
            raise AssertionError("useful assertion message")
    """
    )

    result = pytester.run(sys.executable, "-m", "bistra", "-j", "2", "-q", timeout=30)

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(failed=1, warnings=1)
    result.stdout.fnmatch_lines(
        [
            "*AssertionError: useful assertion message*",
            "*Captured stdout call*",
            "output from test body",
            "*Captured stderr call*",
            "diagnostic from test body",
            "*ApplicationWarning: application warning survived*",
        ]
    )


def test_nested_pytest_run_keeps_reports_and_worker_identity_separate(pytester: pytest.Pytester) -> None:
    nested = pytester.makepyfile(
        inner="""
        import os
        from pathlib import Path
        import pytest

        def test_inner(worker_id):
            assert worker_id == "master"
            assert "PYTEST_XDIST_WORKER" not in os.environ
            Path("inner-isolated").touch()
            pytest.fail("failure belongs to the nested run")
    """
    )
    pytester.makepyfile(
        test_outer=f"""
        import os
        from pathlib import Path
        import pytest

        def test_outer(worker_id):
            assert os.environ["PYTEST_XDIST_WORKER"] == worker_id
            result = pytest.main(["-p", "bistra", "--bistra", "--bistra-workers=1", {str(nested)!r}, "-q"])
            assert result == pytest.ExitCode.TESTS_FAILED
            assert Path("inner-isolated").exists()
            assert os.environ["PYTEST_XDIST_WORKER"] == worker_id
    """
    )

    result = pytester.run(sys.executable, "-m", "bistra", "-j", "2", "test_outer.py", "-q", timeout=30)

    assert result.ret == 0
    result.assert_outcomes(passed=1)


def test_loading_plugin_does_not_enable_parallel_execution(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("""
        import os

        def test_serial(worker_id):
            assert worker_id == "master"
            assert "PYTEST_XDIST_WORKER" not in os.environ
    """)

    result = pytester.run(sys.executable, "-m", "pytest", "-p", "bistra", "-q", timeout=30)

    assert result.ret == 0
    result.assert_outcomes(passed=1)
    assert not (pytester.path / ".bistra_cache").exists()


def test_parametrized_module_fixtures_keep_setup_reuse_and_teardown_order(pytester: pytest.Pytester) -> None:
    pytester.makeconftest("""
        import json
        from pathlib import Path
        import pytest

        @pytest.fixture(scope="module", params=["red", "blue"])
        def resource(request):
            def record(event):
                with Path(f"{request.module.__name__}.events").open("a") as output:
                    output.write(json.dumps([event, request.param]) + "\\n")
            record("setup")
            yield record
            record("teardown")
    """)
    module = """
        def test_first(resource):
            resource("first")

        def test_second(resource):
            resource("second")
    """
    pytester.makepyfile(test_a=module, test_b=module)
    baseline = None
    for invocation in [("pytest",), ("bistra", "-j", "2")]:
        result = pytester.run(sys.executable, "-m", *invocation, "-q", timeout=30)

        assert result.ret == 0
        result.assert_outcomes(passed=8)
        events = {
            path.name: [json.loads(line) for line in path.read_text().splitlines()]
            for path in pytester.path.glob("*.events")
        }
        assert set(events) == {"test_a.events", "test_b.events"}
        if baseline is None:
            baseline = events
        assert events == baseline
        for path in pytester.path.glob("*.events"):
            path.unlink()

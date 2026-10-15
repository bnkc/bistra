from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def run_pytest(tmp_path, *, workers=2, args=(), environment=None):
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env.update(environment or {})
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "bistra.plugin",
            "--bistra",
            "--bistra-workers",
            str(workers),
            "-q",
            *args,
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=25,
    )


def test_original_and_fresh_interpreter_overlap_on_their_main_threads(tmp_path):
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import threading

        def pytest_runtest_logreport(report):
            assert threading.current_thread() is threading.main_thread()
    """)
    )
    (tmp_path / "test_parallel.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import threading
        import time
        import pytest

        COLLECTION_PID = os.getpid()

        @pytest.mark.parametrize("index", range(20))
        def test_execution(index, request, tmp_path_factory):
            assert os.getpid() == COLLECTION_PID
            assert threading.current_thread() is threading.main_thread()
            worker_input = request.config.workerinput
            workerid = worker_input["workerid"]
            assert os.environ["PYTEST_XDIST_WORKER"] == workerid
            assert os.environ["PYTEST_XDIST_WORKER_COUNT"] == "2"
            Path(f"{workerid}-started").touch()
            deadline = time.monotonic() + 5
            while len(list(Path().glob("bistra*-started"))) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(list(Path().glob("bistra*-started"))) == 2
            Path(f"context-{index}.json").write_text(json.dumps({
                "pid": os.getpid(), "workerid": workerid,
                "run_uid": worker_input["testrunuid"],
                "basetemp": str(tmp_path_factory.getbasetemp()),
            }))
    """)
    )
    result = run_pytest(tmp_path, args=("--strict-markers",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "20 passed" in result.stdout
    records = [json.loads(path.read_text()) for path in tmp_path.glob("context-*.json")]
    assert len(records) == 20
    assert len({row["pid"] for row in records}) == 2
    assert {row["workerid"] for row in records} == {"bistra0", "bistra1"}
    assert len({row["run_uid"] for row in records}) == 1
    assert len({row["basetemp"] for row in records}) == 2
    assert len({str(Path(row["basetemp"]).parent) for row in records}) == 1


def test_parent_reports_capture_and_fixture_lifetimes_match_serial(tmp_path):
    (tmp_path / "test_parent.py").write_text(
        textwrap.dedent("""
        import json
        import os
        import threading
        import pytest

        COLLECTION_PID = os.getpid()

        def record(scope, phase):
            with open(os.environ["LIFECYCLE_RECORD"], "a") as output:
                output.write(json.dumps([scope, phase, os.getpid()]) + "\\n")

        @pytest.fixture(scope="session")
        def session_lifetime():
            record("session", "setup")
            yield
            record("session", "teardown")

        @pytest.fixture(scope="module")
        def module_lifetime(session_lifetime):
            record("module", "setup")
            yield
            record("module", "teardown")

        @pytest.fixture
        def teardown_error():
            yield
            print("parent teardown capture")
            raise RuntimeError("parent teardown error")

        @pytest.fixture
        def setup_error():
            print("parent setup capture")
            raise RuntimeError("parent setup error")

        @pytest.mark.parametrize("index", range(3))
        def test_pass(index, module_lifetime, capfd, tmp_path):
            assert os.getpid() == COLLECTION_PID
            assert threading.current_thread() is threading.main_thread()
            print("parent captured stdout")
            os.write(2, b"parent captured stderr\\n")
            captured = capfd.readouterr()
            assert "parent captured stdout" in captured.out
            assert "parent captured stderr" in captured.err
            (tmp_path / "result").write_text(str(index))

        def test_failure(module_lifetime):
            print("parent failure capture")
            assert False, "parent assertion evidence"

        def test_setup_error(setup_error):
            pass

        def test_teardown_error(teardown_error):
            pass

        @pytest.mark.skip(reason="parent skip evidence")
        def test_skip():
            pass

        @pytest.mark.xfail(reason="parent xfail evidence")
        def test_xfail():
            assert False
    """)
    )
    (tmp_path / "test_worker.py").write_text(
        textwrap.dedent("""
        import pytest

        @pytest.mark.parametrize("index", range(4))
        def test_worker(index):
            assert index >= 0
    """)
    )
    outcomes = []
    for workers in (1, 3):
        xml = tmp_path / f"result-{workers}.xml"
        lifecycle = tmp_path / f"lifecycle-{workers}.jsonl"
        result = run_pytest(
            tmp_path,
            workers=workers,
            args=("--strict-markers", f"--junitxml={xml}"),
            environment={"LIFECYCLE_RECORD": str(lifecycle)},
        )
        assert result.returncode == 1, result.stdout + result.stderr
        for evidence in (
            "parent assertion evidence",
            "parent failure capture",
            "parent setup error",
            "parent setup capture",
            "parent teardown error",
            "parent teardown capture",
        ):
            assert evidence in result.stdout
        outcomes.append(
            Counter(
                (
                    case.get("classname"),
                    case.get("name"),
                    tuple(
                        child.tag
                        for child in case
                        if child.tag in ("failure", "error", "skipped")
                    ),
                )
                for case in ET.parse(xml).iter("testcase")
            )
        )
        records = [json.loads(line) for line in lifecycle.read_text().splitlines()]
        assert len({pid for _, _, pid in records}) == 1
        assert [(scope, phase) for scope, phase, _ in records] == [
            ("session", "setup"),
            ("module", "setup"),
            ("module", "teardown"),
            ("session", "teardown"),
        ]
    assert outcomes[0] == outcomes[1]
    assert sum(outcomes[1].values()) == 12


def test_parent_failfast_stops_pending_parent_tests_and_finalizes(tmp_path):
    (tmp_path / "test_parent.py").write_text(
        textwrap.dedent("""
        from pathlib import Path
        import pytest


        @pytest.fixture(scope="module", autouse=True)
        def cleanup():
            yield
            Path("parent-cleaned-up").write_text("yes")

        def test_first_failure(cleanup):
            assert False, "parent failfast evidence"

        def test_must_not_execute():
            Path("unexpected-parent-test").write_text("ran")
    """)
    )
    (tmp_path / "test_worker.py").write_text(
        textwrap.dedent("""
        import pytest

        @pytest.mark.parametrize("index", range(20))
        def test_worker(index):
            assert index >= 0
    """)
    )
    result = run_pytest(tmp_path, args=("-x",))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "parent failfast evidence" in result.stdout
    assert "1 failed" in result.stdout
    assert "bistra worker failure" not in result.stdout
    assert not (tmp_path / "unexpected-parent-test").exists()
    assert (tmp_path / "parent-cleaned-up").read_text() == "yes"

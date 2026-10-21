import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest


def wait_for(path: Path, process: subprocess.Popen, output: Path) -> None:
    deadline = time.monotonic() + 20
    while not path.exists():
        assert process.poll() is None, output.read_text()
        assert time.monotonic() < deadline, f"Timed out waiting for {path.name}\n{output.read_text()}"
        time.sleep(0.01)


def alive(pid: int) -> bool:
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    state = result.stdout.strip()
    return result.returncode == 0 and not state.startswith("Z") and "E" not in state


@contextmanager
def running(pytester: pytest.Pytester, *args: str) -> Iterator[tuple[subprocess.Popen, Path]]:
    pytester.makepyfile(
        record_workers="""
        import os
        from pathlib import Path

        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if worker:
            temporary = Path(f"worker-{worker}.tmp")
            temporary.write_text(str(os.getpid()))
            temporary.replace(f"worker-{worker}.pid")
        """
    )
    output = pytester.path / "output.log"
    with output.open("w") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "bistra", "-j", "2", "-p", "record_workers", *args],
            cwd=pytester.path,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        yield process, output
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        for pid_file in pytester.path.glob("worker-*.pid"):
            pid = int(pid_file.read_text())
            child_file = pid_file.with_name(pid_file.name.replace("worker-", "child-", 1))
            members = [pid]
            if child_file.exists():
                members.append(int(child_file.read_text()))
            if not any(alive(member) for member in members):
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if any(alive(member) for member in members):
                    raise
        process.wait(timeout=10)


def test_worker_resources_outlive_pytest_and_process_cleanup(pytester):
    pytester.makeconftest(
        """
        import atexit
        import os
        from pathlib import Path
        import pytest

        worker = os.environ.get("PYTEST_XDIST_WORKER")
        if worker:
            assert Path(f"resource-{worker}").exists()
            atexit.register(Path(f"exit-{worker}").touch)

        def pytest_configure_node(node):
            Path(f"resource-{node.workerinput['workerid']}").touch()

        def pytest_testnodedown(node, error):
            name = node.workerinput["workerid"]
            assert error is None
            assert Path(f"finalized-{name}").exists()
            assert Path(f"exit-{name}").exists()
            with pytest.raises(ProcessLookupError):
                os.kill(int(Path(f"worker-{name}.pid").read_text()), 0)
            Path(f"resource-{name}").unlink()
            Path(f"down-{name}").touch()

        @pytest.fixture(scope="session", autouse=True)
        def resource():
            yield
            Path(f"finalized-{worker}").touch()
        """
    )
    pytester.makepyfile(
        """
        import time
        from pathlib import Path
        import pytest

        @pytest.mark.parametrize("case", range(2))
        def test_use_resource(case, worker_id):
            Path(f"ready-{worker_id}").touch()
            deadline = time.monotonic() + 15
            while len(list(Path().glob("ready-*"))) != 2:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        """
    )
    with running(pytester) as (process, output):
        assert process.wait(timeout=30) == 0, output.read_text()
        assert len(list(pytester.path.glob("down-*"))) == 2
        assert not list(pytester.path.glob("resource-*"))


def test_partial_worker_provisioning_is_cleaned_up(pytester):
    pytester.makeconftest(
        """
        from pathlib import Path

        def pytest_configure_node(node):
            name = node.workerinput["workerid"]
            Path(f"resource-{name}").touch()
            if name == "bistra1":
                raise RuntimeError("resource provisioning failed")

        def pytest_testnodedown(node, error):
            name = node.workerinput["workerid"]
            Path(f"resource-{name}").unlink()
            Path(f"down-{name}").write_text(str(error))
        """
    )
    pytester.makepyfile("def test_unused(): pass")
    with running(pytester) as (process, output):
        assert process.wait(timeout=30) == pytest.ExitCode.INTERNAL_ERROR, output.read_text()
        assert "resource provisioning failed" in output.read_text()
        assert not list(pytester.path.glob("resource-*"))
        assert len(list(pytester.path.glob("down-*"))) == 2
        assert (pytester.path / "down-bistra1").read_text() == "resource provisioning failed"


def test_worker_startup_failure_preserves_the_import_error(pytester):
    pytester.makeconftest(
        """
        import os
        if os.environ.get("PYTEST_XDIST_WORKER"):
            raise ImportError("project dependency is missing")
        """
    )
    pytester.makepyfile("def test_unused(): pass")
    with running(pytester) as (process, output):
        assert process.wait(timeout=30) != 0
        assert "ImportError: project dependency is missing" in output.read_text()


@pytest.mark.parametrize("stop", ["crash", "interrupt"])
def test_stopping_a_run_cleans_up_workers_and_descendants(pytester, stop):
    pytester.makepyfile(
        child="""
        import os
        import signal
        import sys
        import time
        from pathlib import Path

        if sys.argv[2] == "interrupt":
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        temporary = Path(f"child-{sys.argv[1]}.tmp")
        temporary.write_text(str(os.getpid()))
        temporary.replace(f"child-{sys.argv[1]}.pid")
        Path(f"ready-child-{sys.argv[1]}").touch()
        while True:
            time.sleep(1)
        """,
        test_work=f"""
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path
        import pytest

        @pytest.mark.parametrize("case", range(2))
        def test_running(case, worker_id):
            subprocess.Popen([sys.executable, "child.py", worker_id, {stop!r}])
            deadline = time.monotonic() + 15
            while len(list(Path().glob("ready-child-*"))) != 2:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            Path("ready").touch()
            if {stop!r} == "crash" and case == 0:
                os._exit(17)
            while True:
                time.sleep(1)
        """,
    )
    with running(pytester) as (process, output):
        if stop == "interrupt":
            wait_for(pytester.path / "ready", process, output)
            process.send_signal(signal.SIGINT)
        status = process.wait(timeout=30)
        if stop == "interrupt":
            assert status == pytest.ExitCode.INTERRUPTED, output.read_text()
        else:
            assert status == pytest.ExitCode.INTERNAL_ERROR, output.read_text()
            assert "while running test_work.py::test_running[0]" in output.read_text()
            assert "status 17" in output.read_text()
        assert len(list(pytester.path.glob("child-*.pid"))) == 2
        deadline = time.monotonic() + 10
        pids = [int(path.read_text()) for path in pytester.path.glob("*.pid")]
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not [pid for pid in pids if alive(pid)], output.read_text()


def test_worker_death_is_detected_while_a_descendant_holds_its_connection(pytester):
    pytester.makepyfile(
        test_work="""
        import os
        import signal
        import time
        from pathlib import Path

        def test_crash(worker_id):
            child = Path(f"child-{worker_id}.pid")
            if os.fork() == 0:
                temporary = child.with_suffix(".tmp")
                temporary.write_text(str(os.getpid()))
                temporary.replace(child)
                while True:
                    signal.pause()
            deadline = time.monotonic() + 15
            while not child.exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            os._exit(23)
        """
    )
    with running(pytester) as (process, output):
        assert process.wait(timeout=30) == pytest.ExitCode.INTERNAL_ERROR, output.read_text()
        assert "while running test_work.py::test_crash" in output.read_text()
        assert "status 23" in output.read_text()
        assert len(list(pytester.path.glob("child-*.pid"))) == 1
        pids = [int(path.read_text()) for path in pytester.path.glob("*.pid")]
        deadline = time.monotonic() + 10
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not [pid for pid in pids if alive(pid)], output.read_text()


def test_pytest_exit_preserves_reason_and_status(pytester):
    pytester.makepyfile(
        """
        import pytest

        def test_exit():
            pytest.exit("required service is unavailable", returncode=7)
        """
    )
    with running(pytester) as (process, output):
        assert process.wait(timeout=30) == 7, output.read_text()
        assert "required service is unavailable" in output.read_text()


@pytest.mark.parametrize("at_exit", [False, True], ids=["missing-final-result", "failure-after-final-result"])
def test_worker_exit_requires_a_final_result_and_successful_process_exit(pytester, at_exit):
    pytester.makeconftest("""
        from pathlib import Path

        def pytest_testnodedown(node, error):
            if node.workeroutput.get("ran"):
                Path("final-received").touch()
    """)
    pytester.makepyfile(
        f"""
        import atexit
        import os

        def test_exit(pytestconfig):
            pytestconfig.workeroutput["ran"] = True
            if {at_exit!r}:
                atexit.register(os._exit, 23)
            else:
                os._exit(0)
        """
    )
    with running(pytester) as (process, output):
        assert process.wait(timeout=30) == pytest.ExitCode.INTERNAL_ERROR, output.read_text()
        assert (pytester.path / "final-received").exists() == at_exit
        expected = "status 23" if at_exit else "without a final result"
        assert expected in output.read_text()


def test_fail_fast_allows_an_active_test_to_finish(pytester):
    pytester.makepyfile(
        test_failure="""
        import time
        from pathlib import Path
        import pytest

        pytestmark = pytest.mark.xdist_group("failure")

        def test_fail():
            deadline = time.monotonic() + 15
            while not Path("active").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            pytest.fail("stop here")

        def test_not_started():
            Path("unexpected").touch()
        """,
        test_active="""
        import select
        from pathlib import Path
        import pytest

        pytestmark = pytest.mark.xdist_group("active")

        def test_active(pytestconfig):
            worker = next(
                plugin for plugin in pytestconfig.pluginmanager.get_plugins()
                if type(plugin).__name__ == "WorkerPlugin"
            )
            Path("active").touch()
            # Wait for the stop command without consuming it or racing its delivery.
            assert select.select([worker.channel], [], [], 15)[0]
            Path("completed").touch()

        def test_pending():
            Path("unexpected-peer").touch()
        """,
    )
    with running(pytester, "-x") as (process, output):
        assert process.wait(timeout=30) == pytest.ExitCode.TESTS_FAILED, output.read_text()
        assert "1 failed" in output.read_text()
        assert (pytester.path / "completed").exists()
        assert not (pytester.path / "unexpected").exists()
        assert not (pytester.path / "unexpected-peer").exists()

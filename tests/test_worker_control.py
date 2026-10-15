import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest


def run_bistra(tmp_path, subprocess_env, *args):
    return subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "2", "--", "-q", *args],
        cwd=tmp_path,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.parametrize("returncode", [0, 7, None])
def test_explicit_pytest_exit_preserves_status_and_finalizes(tmp_path, subprocess_env, returncode):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_exit.py").write_text(
        textwrap.dedent(f"""
        from pathlib import Path
        import pytest
        @pytest.fixture
        def cleanup():
            yield
            Path("cleaned-up").write_text("yes")
        def test_exit(cleanup):
            pytest.exit("explicit exit evidence", returncode={returncode!r})
        def test_other():
            pass
    """)
    )
    result = run_bistra(tmp_path, subprocess_env)
    assert result.returncode == (2 if returncode is None else returncode), result.stdout + result.stderr
    assert "explicit exit evidence" in result.stdout + result.stderr
    assert "bistra worker failure" not in result.stdout
    assert (tmp_path / "cleaned-up").read_text() == "yes"


@pytest.mark.parametrize("attribute,returncode", [("shouldstop", 2), ("shouldfail", 1)])
def test_worker_session_stop_is_propagated(tmp_path, subprocess_env, attribute, returncode):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_stop.py").write_text(
        textwrap.dedent(f"""
        import pytest
        @pytest.mark.parametrize("index", range(40))
        def test_stop(index, request):
            with open("executed", "a") as out:
                out.write(str(index) + "\\n")
            if index == 0:
                request.session.{attribute} = "requested worker stop"
    """)
    )
    result = run_bistra(tmp_path, subprocess_env)
    assert result.returncode == returncode, result.stdout + result.stderr
    assert "requested worker stop" in result.stdout + result.stderr
    assert "bistra worker failure" not in result.stdout
    assert len((tmp_path / "executed").read_text().splitlines()) < 40


@pytest.mark.parametrize(
    "args,fragment",
    [
        (["--pdb"], "interactive debugging requires -j 1"),
        (["--trace"], "interactive debugging requires -j 1"),
    ],
)
def test_conflicting_modes_fail_before_execution(tmp_path, subprocess_env, args, fragment):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_unused.py").write_text("def test_unused():\n    raise AssertionError('must not execute')\n")
    result = run_bistra(tmp_path, subprocess_env, *args)
    assert result.returncode == 4, result.stdout + result.stderr
    assert fragment in result.stderr
    assert "must not execute" not in result.stdout


@pytest.mark.skipif(not hasattr(os, "fork"), reason="descendant fault injection requires os.fork")
def test_dead_worker_with_inherited_pipe_cannot_hang_or_orphan_child(tmp_path, subprocess_env):
    pidfile = tmp_path / "descendant.pid"
    stopped = tmp_path / "descendant.stopped"
    source = textwrap.dedent(f"""
        import json
        import multiprocessing
        import os
        from pathlib import Path
        import signal
        import time
        from bistra.executor import _worker, execute_parallel
        pidfile = Path({str(pidfile)!r})
        stopped = Path({str(stopped)!r})
        class ForkAndCrash:
            def initialize(self, worker_id):
                pass
            def execute(self, index, next_index, emit):
                pid = os.fork()
                if pid == 0:
                    def terminate(signum, frame):
                        stopped.write_text("terminated")
                        os._exit(0)
                    signal.signal(signal.SIGTERM, terminate)
                    pidfile.write_text(str(os.getpid()))
                    while True:
                        signal.pause()
                deadline = time.monotonic() + 3
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                os._exit(17)
            def finalize(self, emit):
                pass
        def start_worker(worker_id, initial):
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_worker, args=(child, worker_id, ForkAndCrash(), initial))
            try:
                process.start()
            finally:
                child.close()
            return process, parent
        if __name__ == "__main__":
            result = execute_parallel(
                [0], workers=2, groups=[[0]], start_worker=start_worker,
                parent_execute=lambda index, next_index: None, on_event=lambda event: None,
            )
            deadline = time.monotonic() + 3
            while not stopped.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            print(json.dumps({{"crashes": result.crashes, "stopped": stopped.exists()}}))
    """)
    script = tmp_path / "executor_probe.py"
    script.write_text(source)
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            env=subprocess_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(result.stdout)
        assert len(data["crashes"]) == 1
        assert data["crashes"][0]["exitcode"] == 17
        assert data["stopped"] is True
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

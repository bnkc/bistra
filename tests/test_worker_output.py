import subprocess
import sys
import textwrap


def test_uncaptured_worker_output_reaches_piped_stdout(tmp_path, subprocess_env):
    for index in range(2):
        source = f"def test_output():\n    print('worker-output-{index}')\n"
        (tmp_path / f"test_output_{index}.py").write_text(source)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    args = ["--", "-q", "-s"]
    result = subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "2", *args],
        cwd=tmp_path,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("worker-output-0") == 1
    assert result.stdout.count("worker-output-1") == 1


def test_flushing_workers_does_not_duplicate_parent_output(tmp_path, subprocess_env):
    source = textwrap.dedent("""
        import multiprocessing
        from bistra.executor import _worker, execute_parallel
        class Output:
            def initialize(self, worker_id):
                self.worker_id = worker_id
            def execute(self, index, next_index, emit):
                print("case-output-" + str(index))
            def finalize(self, emit):
                print("finalizer-output-" + str(self.worker_id))
        def start_worker(worker_id, initial):
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_worker, args=(child, worker_id, Output(), initial))
            try:
                process.start()
            finally:
                child.close()
            return process, parent
        if __name__ == "__main__":
            print("parent-output", end="")
            result = execute_parallel(
                [0, 1], workers=3, groups=[[0], [1]], start_worker=start_worker,
                parent_execute=lambda index, next_index: None, on_event=lambda event: None,
            )
            assert not result.crashes
    """)
    script = tmp_path / "executor_probe.py"
    script.write_text(source)
    result = subprocess.run(
        [sys.executable, str(script)],
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for value in (
        "parent-output",
        "case-output-0",
        "case-output-1",
        "finalizer-output-0",
        "finalizer-output-1",
    ):
        assert result.stdout.count(value) == 1


def test_stream_retained_during_collection_stays_open_for_test_logging(tmp_path, subprocess_env):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_logging.py").write_text(
        textwrap.dedent("""
        import sys
        import pytest

        LOG_STREAM = sys.stderr

        @pytest.mark.parametrize("index", range(4))
        def test_logging(index):
            print("retained stream evidence", file=LOG_STREAM)
            assert False, "intentional failure after logging"
    """)
    )
    result = subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "2", "--", "-q"],
        cwd=tmp_path,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "4 failed" in result.stdout
    assert "I/O operation on closed file" not in result.stdout + result.stderr
    assert result.stdout.count("Captured stderr call") == 4


def test_retained_multiprocessing_resources_are_released_at_worker_exit(tmp_path, subprocess_env):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_lock.py").write_text(
        textwrap.dedent("""
        import multiprocessing
        import pytest

        LOCK = multiprocessing.get_context("spawn").Lock()

        @pytest.mark.parametrize("index", range(4))
        def test_lock(index):
            with LOCK:
                assert index >= 0
    """)
    )
    result = subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "2", "--", "-q"],
        cwd=tmp_path,
        env=subprocess_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    assert "resource_tracker:" not in result.stderr

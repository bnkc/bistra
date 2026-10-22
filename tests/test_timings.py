import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from bistra import timings
from bistra.timings import Timing


def test_cache_round_trip_and_environment_separation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert timings.load(tmp_path) == {}
    first = {"test_example.py::test_a": Timing(4, setup=1, teardown=1)}
    timings.save(tmp_path, first)
    key = timings.environment_key()
    cache = tmp_path / ".bistra_cache"

    assert timings.load(tmp_path) == first
    assert json.loads((cache / "timings.json").read_text()) == {
        "version": 2,
        "environments": {key: {"test_example.py::test_a": {"total": 4, "setup": 1, "teardown": 1}}},
    }
    assert (cache / ".gitignore").read_text() == "*\n"

    with monkeypatch.context() as other_environment:
        other_environment.setattr(sys, "prefix", str(tmp_path / "another-venv"))
        assert timings.load(tmp_path) == {}
        timings.save(tmp_path, {"other": Timing(10)})
        assert timings.load(tmp_path) == {"other": Timing(10)}

    assert timings.load(tmp_path) == first
    assert len(json.loads((cache / "timings.json").read_text())["environments"]) == 2


def test_updates_average_measurements_and_retain_unmeasured_tests_and_phases(tmp_path: Path) -> None:
    untouched = Timing(7, setup=1, teardown=1)
    timings.save(tmp_path, {"untouched": untouched, "updated": Timing(8, setup=2, teardown=4)})

    timings.save(tmp_path, {"updated": Timing(4, teardown=2), "new": Timing(3)})

    assert timings.load(tmp_path) == {
        "untouched": untouched,
        "updated": Timing(6, setup=2, teardown=3),
        "new": Timing(3),
    }


def test_saving_old_history_drops_call_without_losing_other_measurements(tmp_path: Path) -> None:
    path = tmp_path / ".bistra_cache" / "timings.json"
    path.parent.mkdir()
    record = {"total": 9, "setup": 2, "call": 4, "teardown": 1}
    key = timings.environment_key()
    path.write_text(
        json.dumps({"version": 2, "environments": {key: {"unselected": record}, "other-env": {"other": record}}})
    )

    assert timings.load(tmp_path) == {"unselected": Timing(9, setup=2, teardown=1)}

    timings.save(tmp_path, {"selected": Timing(3)})

    kept = {"total": 9, "setup": 2, "teardown": 1}
    assert json.loads(path.read_text()) == {
        "version": 2,
        "environments": {
            key: {"unselected": kept, "selected": {"total": 3}},
            "other-env": {"other": kept},
        },
    }


@pytest.mark.parametrize("contents", [b"{", b"\xff", b"[]", b'{"version": 1, "environments": {}}'])
def test_unusable_cache_is_a_miss_and_can_be_replaced(tmp_path: Path, contents: bytes) -> None:
    cache = tmp_path / ".bistra_cache"
    cache.mkdir()
    (cache / "timings.json").write_bytes(contents)

    assert timings.load(tmp_path) == {}
    timings.save(tmp_path, {"test_a": Timing(1)})
    assert timings.load(tmp_path) == {"test_a": Timing(1)}


def test_bad_measurements_do_not_discard_valid_history(tmp_path: Path) -> None:
    cache = tmp_path / ".bistra_cache"
    cache.mkdir()
    (cache / "timings.json").write_text(
        json.dumps(
            {
                "version": 2,
                "environments": {
                    timings.environment_key(): {
                        "valid": {"total": 5, "setup": -1, "teardown": "unknown"},
                        "negative": {"total": -1},
                        "infinite": {"total": float("inf")},
                        "boolean": {"total": True},
                        "wrong-shape": [],
                    }
                },
            }
        )
    )

    assert timings.load(tmp_path) == {"valid": Timing(5)}


def test_failed_atomic_replace_preserves_previous_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timings.save(tmp_path, {"old": Timing(1)})
    cache = tmp_path / ".bistra_cache"
    previous = (cache / "timings.json").read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("cannot replace cache")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="cannot replace cache"):
        timings.save(tmp_path, {"new": Timing(2)})

    assert (cache / "timings.json").read_bytes() == previous
    assert timings.load(tmp_path) == {"old": Timing(1)}
    assert list(cache.glob("*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX file locks to coordinate the writers")
def test_concurrent_writers_merge_under_the_same_lock(tmp_path: Path) -> None:
    first_writer = """
import sys
from pathlib import Path
from bistra import timings

write = timings._write

def paused_write(path, data):
    print('ready', flush=True)
    assert sys.stdin.readline() == 'go\\n'
    write(path, data)

timings._write = paused_write
timings.save(Path(sys.argv[1]), {'first': timings.Timing(2)})
"""
    second_writer = """
import fcntl
import sys
from contextlib import contextmanager
from pathlib import Path
from bistra import timings

locked = timings._locked

@contextmanager
def checked_lock(path):
    with path.open('a+b') as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('first writer did not hold the cache lock')
    print('ready', flush=True)
    with locked(path):
        yield

timings._locked = checked_lock
timings.save(Path(sys.argv[1]), {'second': timings.Timing(3)})
"""
    processes: list[subprocess.Popen[str]] = []
    try:
        for code in (first_writer, second_writer):
            process = subprocess.Popen(
                [sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
            )
            processes.append(process)
            assert process.stdout is not None
            assert select.select([process.stdout], [], [], 10)[0], "writer did not become ready"
            assert process.stdout.readline() == "ready\n"
        assert processes[0].stdin is not None
        processes[0].stdin.write("go\n")
        processes[0].stdin.flush()
        for process in processes:
            assert process.wait(timeout=10) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    assert timings.load(tmp_path) == {"first": Timing(2), "second": Timing(3)}

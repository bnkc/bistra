import multiprocessing
import os
import threading
import time

import pytest
from executor_helpers import unexpected_parent, worker_starter

from bistra.executor import WorkerStop, execute_parallel


@pytest.mark.parametrize("states", ["42 Z\n77 S\n", "77 S\n"])
def test_permission_error_for_a_dead_group_is_not_a_cleanup_failure(monkeypatch, states):
    from bistra import executor

    def denied(*args):
        raise PermissionError("group cannot be signalled")

    monkeypatch.setattr(executor.os, "killpg", denied)
    monkeypatch.setattr(executor.subprocess, "check_output", lambda *a, **k: states)
    assert executor._signal_group(42, 0) is False


def test_permission_error_for_a_live_group_is_not_hidden(monkeypatch):
    from bistra import executor

    def denied(*args):
        raise PermissionError("real permission failure")

    monkeypatch.setattr(executor.os, "killpg", denied)
    monkeypatch.setattr(executor.subprocess, "check_output", lambda *a, **k: "42 Z\n42 S\n")
    with pytest.raises(PermissionError, match="real permission failure"):
        executor._signal_group(42, 0)


class Recorder:
    def initialize(self, worker_id):
        self.worker_id = worker_id

    def execute(self, index, next_index, emit):
        emit({"type": "value", "value": index, "next": next_index, "pid": os.getpid()})

    def finalize(self, emit):
        emit({"type": "finalized"})


def parent_recorder(on_event, worker_id):
    def execute(index, next_index):
        event = {
            "type": "value",
            "value": index,
            "next": next_index,
            "pid": os.getpid(),
            "worker_id": worker_id,
            "index": index,
        }
        if on_event(event) is False:
            raise WorkerStop("parent callback requested stop", completed=True)

    return execute


class Crasher(Recorder):
    def execute(self, index, next_index, emit):
        if index == 0:
            os._exit(17)
        super().execute(index, next_index, emit)


class BrokenFinalizer(Recorder):
    def finalize(self, emit):
        emit({"type": "finalizer_called"})
        raise RuntimeError("intentional finalizer failure")


class StopWithBrokenCleanup(BrokenFinalizer):
    def execute(self, index, next_index, emit):
        raise WorkerStop("intentional early stop")


def test_cleanup_error_during_controlled_stop_is_a_crash():
    events = []
    result = execute_parallel(
        [0],
        workers=2,
        groups=[[0]],
        start_worker=worker_starter(StopWithBrokenCleanup()),
        parent_execute=unexpected_parent,
        on_event=events.append,
    )
    assert not result.completed
    assert len(result.crashes) == 1
    assert "intentional finalizer failure" in result.crashes[0]["traceback"]
    assert [e["type"] for e in events].count("finalizer_called") == 1


def test_failed_finalizer_is_not_invoked_twice():
    events = []
    result = execute_parallel(
        [0],
        workers=2,
        groups=[[0]],
        start_worker=worker_starter(BrokenFinalizer()),
        parent_execute=unexpected_parent,
        on_event=events.append,
    )
    assert result.completed == {0}
    assert len(result.crashes) == 1
    assert "intentional finalizer failure" in result.crashes[0]["traceback"]
    assert [e["type"] for e in events].count("finalizer_called") == 1


def test_exactly_once_and_worker_lookahead():
    events = []
    completions = []
    result = execute_parallel(
        list(range(37)),
        workers=3,
        groups=[list(range(start, min(start + 4, 37))) for start in range(0, 37, 4)],
        start_worker=worker_starter(Recorder()),
        parent_execute=parent_recorder(events.append, 2),
        on_event=events.append,
        on_complete=lambda index, elapsed: completions.append((index, elapsed)),
    )
    assert result.completed == set(range(37))
    assert not result.crashes
    assert sorted(i for i, _ in completions) == list(range(37))
    values = [e for e in events if e["type"] == "value"]
    assert sorted(e["value"] for e in values) == list(range(37))
    assert any(e["pid"] == os.getpid() for e in values)
    assert any(e["pid"] != os.getpid() for e in values)
    for worker in {e["worker_id"] for e in values}:
        sequence = [e for e in values if e["worker_id"] == worker]
        assert [e["next"] for e in sequence] == [e["value"] for e in sequence[1:]] + [None]
    assert len([e for e in events if e["type"] == "finalized"]) == 2


def test_crashed_worker_cannot_appear_successful():
    result = execute_parallel(
        list(range(12)),
        workers=2,
        groups=[list(range(12))],
        start_worker=worker_starter(Crasher()),
        parent_execute=unexpected_parent,
        on_event=lambda event: None,
    )
    assert result.crashes
    assert 0 not in result.completed
    assert any(0 in crash["uncompleted"] for crash in result.crashes)


def test_failfast_accounts_for_completed_work():
    events = []

    def stop(event):
        events.append(event)
        return False

    result = execute_parallel(
        list(range(100)),
        workers=2,
        groups=[[index] for index in range(100)],
        start_worker=worker_starter(Recorder()),
        parent_execute=parent_recorder(stop, 1),
        on_event=stop,
    )
    assert result.stopped
    assert len(result.completed) < 100
    assert result.completed == {e["value"] for e in events if e["type"] == "value"}


def test_dispatch_groups_preserve_local_order_and_exact_lookahead():
    groups = [[6, 7, 8], [3, 4, 5], [0, 1, 2], [9]]
    events = []
    result = execute_parallel(
        list(range(10)),
        workers=3,
        groups=groups,
        start_worker=worker_starter(Recorder()),
        parent_execute=parent_recorder(events.append, 2),
        on_event=events.append,
    )
    assert result.completed == set(range(10))
    assert not result.crashes
    values = [event for event in events if event["type"] == "value"]
    assert sorted(event["value"] for event in values) == list(range(10))
    for group in groups:
        records = [event for event in values if event["value"] in group]
        assert [event["value"] for event in records] == group
        assert len({event["worker_id"] for event in records}) == 1
    for worker in {event["worker_id"] for event in values}:
        records = [event for event in values if event["worker_id"] == worker]
        assert [event["next"] for event in records] == [event["value"] for event in records[1:]] + [None]
    assert groups == [[6, 7, 8], [3, 4, 5], [0, 1, 2], [9]]


def test_group_order_refers_to_supplied_indices_not_numeric_sort():
    events = []
    result = execute_parallel(
        [5, 1, 8, 2, 9],
        workers=2,
        groups=[[5, 8, 9], [1, 2]],
        start_worker=worker_starter(Recorder()),
        parent_execute=parent_recorder(events.append, 1),
        on_event=events.append,
    )
    assert result.completed == {5, 1, 8, 2, 9}
    assert [e["value"] for e in events if e["type"] == "value" and e["worker_id"] == 0] == [5, 8, 9]


@pytest.mark.parametrize(
    "groups,fragment",
    [
        ([[0, 1], [1, 2]], "every job index exactly once"),
        ([[0], [1]], "every job index exactly once"),
        ([[0, 1], [2, 9]], "every job index exactly once"),
        ([[1, 0], [2]], "preserve the original indices order"),
        ([[0], [], [1, 2]], "must not be empty"),
    ],
)
def test_invalid_groups_are_rejected_before_starting_workers(groups, fragment):
    with pytest.raises(ValueError, match=fragment):
        execute_parallel(
            [0, 1, 2],
            workers=2,
            groups=groups,
            start_worker=lambda *args: pytest.fail("invalid groups must not start a worker"),
            parent_execute=unexpected_parent,
            on_event=lambda event: None,
        )


def _wait_for_file(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError(f"timed out waiting for {path.name}")
        time.sleep(0.005)


class OverlapRecorder(Recorder):
    def __init__(self, directory):
        self.directory = directory

    def execute(self, index, next_index, emit):
        (self.directory / f"child-{self.worker_id}").touch()
        _wait_for_file(self.directory / "parent")
        super().execute(index, next_index, emit)


def test_balanced_queues_preserve_affinity_and_lookahead_for_parent_and_child():
    events = []
    parent_calls = []
    completions = []
    groups = [[0], [1], [2, 3], [4]]
    result = execute_parallel(
        list(range(5)),
        workers=2,
        groups=groups,
        estimates={0: 1, 1: 100, 2: 3, 3: 3, 4: 0.001},
        start_worker=worker_starter(Recorder()),
        parent_execute=lambda index, next_index: parent_calls.append((index, next_index)),
        on_event=events.append,
        on_complete=lambda index, elapsed: completions.append(index),
    )
    assert result.completed == set(range(5))
    assert not result.crashes
    assert sorted(completions) == list(range(5))
    assert parent_calls == [(0, 2), (2, 3), (3, 4), (4, None)]
    values = [event for event in events if event["type"] == "value"]
    assert [(event["value"], event["next"]) for event in values] == [(1, None)]
    assert values[0]["pid"] != os.getpid()
    assert groups == [[0], [1], [2, 3], [4]]


@pytest.mark.parametrize("reverse_priority", [False, True])
def test_parent_overlaps_children_and_callbacks_stay_on_main_thread(tmp_path, reverse_priority, monkeypatch):
    caller = threading.get_ident()
    parent_pid = os.getpid()
    indices = list(range(50))
    events = []
    parent_calls = []
    completions = []
    starts = []
    original_start = multiprocessing.process.BaseProcess.start
    existing_threads = set(threading.enumerate())

    def process_start(process):
        starts.append(threading.get_ident())
        # The outer Bistra run may already have its own coordinator.
        assert not any(thread.name == "bistra-coordinator" for thread in set(threading.enumerate()) - existing_threads)
        return original_start(process)

    monkeypatch.setattr(multiprocessing.process.BaseProcess, "start", process_start)

    def parent_execute(index, next_index):
        assert threading.get_ident() == caller
        assert starts == [caller, caller]
        parent_calls.append((index, next_index, os.getpid()))
        if len(parent_calls) == 1:
            (tmp_path / "parent").touch()
            _wait_for_file(tmp_path / "child-0")
            _wait_for_file(tmp_path / "child-1")

    def event(value):
        assert threading.get_ident() == caller
        events.append(value)

    def complete(index, elapsed):
        assert threading.get_ident() == caller
        completions.append(index)

    groups = [[index] for index in (reversed(indices) if reverse_priority else indices)]
    result = execute_parallel(
        indices,
        workers=3,
        groups=groups,
        start_worker=worker_starter(OverlapRecorder(tmp_path)),
        parent_execute=parent_execute,
        on_event=event,
        on_complete=complete,
    )
    assert result.completed == set(indices)
    assert not result.crashes
    assert sorted(completions) == indices
    assert all(call[2] == parent_pid for call in parent_calls)
    parent_order = [call[0] for call in parent_calls]
    assert [call[1] for call in parent_calls] == parent_order[1:] + [None]
    values = [event for event in events if event["type"] == "value"]
    assert all(event["pid"] != parent_pid for event in values)
    assert sorted(parent_order + [event["value"] for event in values]) == indices
    for worker in {event["worker_id"] for event in values}:
        sequence = [event for event in values if event["worker_id"] == worker]
        assert [event["next"] for event in sequence] == [event["value"] for event in sequence[1:]] + [None]


@pytest.mark.parametrize("completed", [False, True])
def test_parent_controlled_stop_accounts_for_completion(completed):
    parent_calls = []

    def parent_execute(index, next_index):
        parent_calls.append(index)
        raise WorkerStop("parent requested stop", completed=completed)

    result = execute_parallel(
        list(range(20)),
        workers=2,
        groups=[[index] for index in range(20)],
        start_worker=worker_starter(Recorder()),
        parent_execute=parent_execute,
        on_event=lambda event: None,
    )
    assert result.stopped
    assert not result.crashes
    assert len(parent_calls) == 1
    assert (parent_calls[0] in result.completed) is completed


class WaitingRecorder(Recorder):
    def __init__(self, pidfile):
        self.pidfile = pidfile

    def execute(self, index, next_index, emit):
        self.pidfile.write_text(str(os.getpid()))
        time.sleep(30)


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_parent_exception_cleans_up_active_children(tmp_path, error):
    pidfile = tmp_path / "worker-pid"

    def parent_execute(index, next_index):
        _wait_for_file(pidfile)
        raise error("parent failed")

    with pytest.raises(error, match="parent failed"):
        execute_parallel(
            [0, 1],
            workers=2,
            groups=[[0], [1]],
            start_worker=worker_starter(WaitingRecorder(pidfile)),
            parent_execute=parent_execute,
            on_event=lambda event: None,
        )
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_one_slot_runs_all_jobs_in_parent():
    calls = []
    result = execute_parallel(
        [0, 1, 2, 3],
        workers=1,
        groups=[[0, 1, 2, 3]],
        start_worker=lambda *args: pytest.fail("one execution slot must not start a child"),
        parent_execute=lambda index, next_index: calls.append((index, next_index)),
        on_event=lambda event: pytest.fail("no child worker should run"),
    )
    assert result.completed == {0, 1, 2, 3}
    assert calls == [(0, 1), (1, 2), (2, 3), (3, None)]


def test_child_crash_remains_a_failure_with_parent_execution():
    result = execute_parallel(
        list(range(20)),
        workers=2,
        groups=[[index] for index in range(20)],
        start_worker=worker_starter(Crasher()),
        parent_execute=lambda index, next_index: None,
        on_event=lambda event: None,
    )
    assert result.crashes
    assert 0 not in result.completed
    assert any(0 in crash["uncompleted"] for crash in result.crashes)

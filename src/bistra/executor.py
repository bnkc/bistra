"""Run grouped jobs across the parent process and child workers.

Workers exchange job indices and events. Looking one job ahead preserves
fixture scopes between groups.
"""

import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from queue import Empty, SimpleQueue
from typing import Literal, cast

from .protocol import Event, OnEvent, StartWorker, WorkerAdapter, WorkerFailure, WorkerMessage
from .scheduling import plan_workers
from .transport import FramedReceiver, TransportError

_clock = time.perf_counter


def _signal_group(pgid: int, signum: int) -> bool:
    """Signal a worker group; macOS can return EPERM for zombie-only groups."""
    try:
        os.killpg(pgid, signum)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Ignore EPERM only when every process in the group is gone or a zombie.
        rows = subprocess.check_output(["ps", "-axo", "pgid=,stat="], text=True)
        for row in rows.splitlines():
            group, state = row.split()
            if int(group) == pgid and not state.startswith("Z"):
                raise
        return False


@dataclass
class PoolResult:
    completed: set[int] = field(default_factory=set)
    crashes: list[WorkerFailure] = field(default_factory=list)
    stopped: bool = False


class WorkerStop(Exception):
    """An adapter requested an early stop.

    Set completed=True if the current job finished before the stop request.
    """

    def __init__(self, reason: str, *, completed: bool = False) -> None:
        super().__init__(reason)
        self.completed = completed


def _finish_worker() -> None:
    # This private cleanup hook is missing from typeshed.
    from multiprocessing.util import _run_finalizers  # ty: ignore[unresolved-import]

    # os._exit skips normal cleanup, so release queues and named semaphores here.
    _run_finalizers()
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()


def _message_parts(message: object) -> WorkerMessage:
    """Check the worker message format before passing events to callbacks."""
    if not isinstance(message, tuple) or len(message) != 2:
        raise TransportError("invalid worker message envelope")
    kind, payload = message
    if not isinstance(kind, str):
        raise TransportError("invalid worker message kind")
    events = None
    valid = True
    if kind == "need":
        valid = payload is None
    elif kind == "started":
        valid = type(payload) is int
    elif kind == "done":
        valid = (
            isinstance(payload, tuple)
            and len(payload) == 3
            and type(payload[0]) is int
            and isinstance(payload[1], (int, float))
        )
        if valid:
            events = payload[2]
    elif kind == "finished":
        events = payload
    elif kind in ("crash", "stopped"):
        valid = isinstance(payload, dict)
        if valid:
            events = payload.get("events", [])
    else:
        raise TransportError(f"unknown worker message: {kind}")
    if kind in ("done", "finished", "crash", "stopped") and valid:
        valid = isinstance(events, list) and all(isinstance(event, dict) for event in events)
    if not valid:
        raise TransportError(f"invalid payload for worker message: {kind}")
    return cast(WorkerMessage, message)


def _worker(connection: Connection, worker_id: int, adapter: WorkerAdapter, initial: list[int]) -> None:
    pending: deque[int] = deque(initial)
    final = False
    index: int | None = None
    events: list[Event] = []

    def emit(event: Event) -> None:
        event.setdefault("worker_id", worker_id)
        event.setdefault("index", index)
        events.append(event)

    def fetch() -> None:
        nonlocal final
        connection.send(("need", None))
        batch = connection.recv()
        if batch is None:
            final = True
        else:
            pending.extend(batch)

    failed = False
    initialized = False
    cleanup_attempted = False
    try:
        # A separate process group lets the parent clean up worker subprocesses.
        if os.getsid(0) != os.getpid():
            os.setsid()
        adapter.initialize(worker_id)
        initialized = True
        while pending:
            if len(pending) == 1 and not final:
                fetch()
            index = pending.popleft()
            next_index = pending[0] if pending else None
            connection.send(("started", index))
            start = _clock()
            adapter.execute(index, next_index, emit)
            elapsed = _clock() - start
            connection.send(("done", (index, elapsed, events)))
            events = []
        if initialized:
            initialized = False
            adapter.finalize(emit)
        # Flush uncaptured output before exiting.
        cleanup_attempted = True
        _finish_worker()
        connection.send(("finished", events))
    except BaseException as error:
        controlled = isinstance(error, WorkerStop)
        failed = not controlled
        detail = str(error) if controlled else traceback.format_exc()
        if isinstance(error, WorkerStop) and error.completed:
            connection.send(("done", (index, _clock() - start, events)))
            events = []
        if initialized:
            initialized = False
            try:
                adapter.finalize(emit)
            except BaseException:
                failed = True
                detail += "\nDuring worker cleanup:\n" + traceback.format_exc()
        if not cleanup_attempted:
            try:
                _finish_worker()
            except BaseException:
                failed = True
                detail += "\nFinalizing worker resources:\n" + traceback.format_exc()
        try:
            kind = "crash" if failed else "stopped"
            connection.send(
                (
                    kind,
                    {
                        "worker_id": worker_id,
                        "index": index,
                        "traceback": detail,
                        "events": events,
                    },
                )
            )
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
        # Avoid running session cleanup again when the outer pytest.main returns.
        os._exit(1 if failed else 0)


def execute_parallel(
    indices: list[int],
    *,
    workers: int,
    groups: list[list[int]],
    start_worker: StartWorker,
    parent_execute: Callable[[int, int | None], None] | None = None,
    on_event: OnEvent,
    on_complete: Callable[[int, float], None] | None = None,
    estimates: Mapping[int, object] | None = None,
) -> PoolResult:
    """Run duration-balanced assignments in collection order on each worker.

    Fixture groups constrain assignment, not execution order. Worker queues are
    fixed before execution; batches preserve lookahead and bounded stop handling.

    The worker count includes the calling process only when parent_execute is
    supplied. Otherwise all execution slots are children. Callbacks run on the calling
    thread while a coordinator handles child messages. start_worker(worker_id,
    initial) returns a ready process and connection, reusing workers that have
    already collected their tests.
    """
    if workers < 1:
        raise ValueError("workers must be positive")
    if len(set(indices)) != len(indices):
        raise ValueError("job indices must be unique")
    positions = {index: rank for rank, index in enumerate(indices)}
    if any(not group for group in groups):
        raise ValueError("dispatch groups must not be empty")
    flat = [index for group in groups for index in group]
    if len(flat) != len(indices) or set(flat) != set(indices):
        raise ValueError("groups must contain every job index exactly once")
    for group in groups:
        ranks = [positions[index] for index in group]
        if ranks != sorted(ranks):
            raise ValueError("each group must preserve the original indices order")
    plans = [deque(batches) for batches in plan_workers(indices, groups, workers, estimates or {})]
    parent_group: deque[int] = deque()
    result = PoolResult()
    if not indices:
        return result
    processes: dict[Connection, BaseProcess] = {}
    receivers: dict[Connection, FramedReceiver] = {}
    active: dict[Connection, int | None] = {}
    assigned: dict[object, set[int]] = {}
    queues: dict[object, deque[list[int]]] = {}
    parent_slot = object()
    assigned[parent_slot] = set()
    stopping = False
    state_lock = threading.Lock()
    abort = threading.Event()
    callbacks: SimpleQueue[tuple[Literal["events"], list[Event]] | tuple[Literal["complete"], tuple[int, float]]] = (
        SimpleQueue()
    )
    coordinator: threading.Thread | None = None
    coordinator_errors: list[BaseException] = []

    def deliver(events: list[Event]) -> None:
        nonlocal stopping
        for event in events:
            if on_event(event) is False:
                stopping = result.stopped = True

    def worker_events(events: list[Event]) -> None:
        callbacks.put(("events", events))

    def worker_completed(index: int, elapsed: float) -> None:
        if on_complete is not None:
            callbacks.put(("complete", (index, elapsed)))

    def drain_callbacks() -> None:
        while True:
            try:
                callback = callbacks.get_nowait()
            except Empty:
                return
            if callback[0] == "events":
                deliver(callback[1])
            else:
                assert on_complete is not None
                on_complete(*callback[1])

    def take_group(owner: object) -> list[int] | None:
        queue = queues.get(owner)
        return queue.popleft() if queue else None

    def take_parent_job() -> int | None:
        with state_lock:
            if stopping:
                return None
            if not parent_group:
                group = take_group(parent_slot)
                if group:
                    parent_group.extend(group)
                    assigned[parent_slot].update(group)
            return parent_group.popleft() if parent_group else None

    def stop_processes(signum: int) -> list[int]:
        signalled = []
        for process in processes.values():
            assert process.pid is not None
            # Clean up the whole group, even if its worker has already crashed.
            if _signal_group(process.pid, signum):
                signalled.append(process.pid)
            else:
                # A child interrupted before setsid still needs termination.
                if not process.is_alive():
                    continue
                try:
                    os.kill(process.pid, signum)
                except ProcessLookupError:
                    pass
        return signalled

    def worker_disappeared(connection: Connection, detail: str | None = None) -> None:
        nonlocal stopping
        process = processes[connection]
        process.join(timeout=0.1)
        result.crashes.append(
            {
                "index": active[connection],
                "exitcode": process.exitcode,
                "uncompleted": sorted(assigned[connection]),
                "traceback": detail or receivers[connection].incomplete or "worker exited without a terminal result",
            }
        )
        del active[connection]
        stopping = True

    def send_batch(connection: Connection, batch: list[int] | None) -> None:
        try:
            connection.send(batch)
        except OSError as error:
            raise TransportError(f"cannot send worker batch: {error}") from error

    def coordinate() -> None:
        nonlocal stopping
        while active and not abort.is_set():
            sentinels = {processes[connection].sentinel: connection for connection in active}
            ready = wait([*active, *sentinels], timeout=0.25)
            # Read pending messages first: a worker sends its final result before exiting.
            for connection in [cast(Connection, item) for item in ready if item in active]:
                try:
                    for message in receivers[connection].receive():
                        message = _message_parts(message)
                        if message[0] == "need":
                            with state_lock:
                                if stopping:
                                    batch = None
                                else:
                                    batch = take_group(connection)
                                if batch:
                                    assigned[connection].update(batch)
                            send_batch(connection, batch)
                        elif message[0] == "started":
                            payload = message[1]
                            if payload not in assigned[connection]:
                                raise TransportError(f"worker started unassigned job {payload}")
                            active[connection] = payload
                        elif message[0] == "done":
                            index, elapsed, events = message[1]
                            if index in result.completed or index not in assigned[connection]:
                                raise TransportError(f"worker completed unassigned or repeated job {index}")
                            with state_lock:
                                result.completed.add(index)
                                assigned[connection].discard(index)
                            worker_events(events)
                            worker_completed(index, elapsed)
                        elif message[0] == "finished":
                            if assigned[connection]:
                                raise TransportError("worker finished with unaccounted jobs")
                            worker_events(message[1])
                            del active[connection]
                        elif message[0] == "crash":
                            failure = message[1]
                            worker_events(failure.pop("events", []))
                            failure["uncompleted"] = sorted(assigned[connection])
                            result.crashes.append(failure)
                            del active[connection]
                            stopping = True
                        elif message[0] == "stopped":
                            worker_events(message[1].pop("events", []))
                            result.stopped = stopping = True
                            del active[connection]
                        if connection not in active:
                            break
                except TransportError as error:
                    if connection in active:
                        worker_disappeared(connection, f"worker transport error: {error}")
                if connection in active and receivers[connection].eof:
                    worker_disappeared(connection)
            for sentinel in [item for item in ready if item in sentinels]:
                connection = sentinels[sentinel]
                if connection in active and not connection.poll():
                    # A forked descendant can keep the pipe open after its worker dies.
                    worker_disappeared(connection)
            # Descendants can inherit both the sentinel and result pipes.
            # exitcode polls waitpid, so it detects death even if both stay open.
            for connection in list(active):
                if processes[connection].exitcode is not None and not connection.poll():
                    worker_disappeared(connection)

    def coordinate_in_thread() -> None:
        nonlocal stopping
        try:
            coordinate()
        except BaseException as error:
            coordinator_errors.append(error)
            stopping = True

    def run_parent() -> None:
        nonlocal stopping
        index = take_parent_job() if parent_execute is not None else None
        while index is not None:
            drain_callbacks()
            if stopping:
                break
            next_index = take_parent_job()
            start = _clock()
            complete = True
            try:
                assert parent_execute is not None
                parent_execute(index, next_index)
            except WorkerStop as error:
                complete = error.completed
                stopping = result.stopped = True
            if complete:
                with state_lock:
                    result.completed.add(index)
                    assigned[parent_slot].discard(index)
                if on_complete is not None:
                    on_complete(index, _clock() - start)
            drain_callbacks()
            if stopping:
                break
            index = next_index
        if coordinator is not None:
            while coordinator.is_alive():
                drain_callbacks()
                coordinator.join(timeout=0.025)
        drain_callbacks()
        if coordinator_errors:
            raise coordinator_errors[0]

    try:
        child_count = min(workers - (parent_execute is not None), len(plans))
        for worker_id in range(child_count):
            queue = plans[worker_id]
            initial = queue.popleft()
            process, parent = start_worker(worker_id, initial)
            processes[parent] = process
            active[parent] = None
            assigned[parent] = set(initial)
            queues[parent] = queue
        # Read without blocking while keeping Connection.send blocking.
        receivers = {connection: FramedReceiver(connection) for connection in active}
        if parent_execute is not None and child_count < len(plans):
            queues[parent_slot] = plans[child_count]
        if active:
            coordinator = threading.Thread(target=coordinate_in_thread, name="bistra-coordinator")
            coordinator.start()
        run_parent()
    except KeyboardInterrupt:
        result.stopped = True
        stop_processes(signal.SIGINT)
        raise
    finally:
        abort.set()
        if coordinator is not None:
            coordinator.join()
        for process in processes.values():
            process.join(timeout=0.5)
        signalled = stop_processes(signal.SIGTERM)
        for process in processes.values():
            process.join(timeout=1)
        if signalled:
            # Give descendants time to handle SIGTERM, even if their worker has
            # already exited and join() returns immediately.
            deadline = _clock() + 0.25
            while signalled and _clock() < deadline:
                live_groups = []
                for pid in signalled:
                    if _signal_group(pid, 0):
                        live_groups.append(pid)
                signalled = live_groups
                if signalled:
                    time.sleep(0.01)
        stop_processes(signal.SIGKILL)
        for process in processes.values():
            process.join(timeout=1)
        for receiver in receivers.values():
            receiver.close()
        for connection in processes:
            connection.close()
    missing = set(indices) - result.completed
    if missing and not result.stopped and not result.crashes:
        raise RuntimeError(f"executor lost {len(missing)} jobs")
    return result

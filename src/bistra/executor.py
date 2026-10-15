"""Process executor, independent of any test framework.

Only integer job indices and result events cross process boundaries. A one-job lookahead lets adapters preserve
fixture scopes across batch boundaries.
"""

from __future__ import annotations

import math
import multiprocessing
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
from multiprocessing.connection import wait
from queue import Empty, SimpleQueue
from typing import Any

from .scheduling import GroupQueue, estimate_durations
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
        # Verify the group has no live members before treating EPERM as gone.
        # A real permission failure on a running process must still propagate.
        rows = subprocess.check_output(["ps", "-axo", "pgid=,stat="], text=True)
        for row in rows.splitlines():
            group, state = row.split()
            if int(group) == pgid and not state.startswith("Z"):
                raise
        return False


@dataclass
class PoolResult:
    completed: set[int] = field(default_factory=set)
    crashes: list[dict[str, Any]] = field(default_factory=list)
    stopped: bool = False


class WorkerStop(Exception):
    """An adapter requested normal early termination of this worker.

    completed distinguishes a completed protocol followed by a stop condition
    from an exception that aborted the current job.
    """

    def __init__(self, reason: str, *, completed: bool = False):
        super().__init__(reason)
        self.completed = completed


def _finish_worker() -> None:
    from multiprocessing.util import _run_finalizers

    # The hard exit below bypasses multiprocessing's interpreter cleanup.
    # Release retained queues and named semaphores before reporting completion.
    _run_finalizers()
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()


def _message_parts(message):
    """Validate wire structure separately from user callbacks."""
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
        valid = isinstance(events, list) and all(
            isinstance(event, dict) for event in events
        )
    if not valid:
        raise TransportError(f"invalid payload for worker message: {kind}")
    return kind, payload


def _worker(connection, worker_id: int, adapter, initial: list[int]) -> None:
    pending: deque[int] = deque(initial)
    final = False
    index = None
    events: list[dict] = []

    def emit(event: dict) -> None:
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
        # A worker has its own process group so interrupted descendant processes
        # cannot be left behind when a suite launches subprocesses.
        if os.getsid(0) != os.getpid():
            os.setsid()
        adapter.initialize(worker_id)
        initialized = True
        while pending:
            if (
                len(pending) == 1
                and not final
                and getattr(adapter, "needs_lookahead", True)
            ):
                fetch()
            index = pending.popleft()
            next_index = pending[0] if pending else None
            connection.send(("started", index))
            start = _clock()
            adapter.execute(index, next_index, emit)
            elapsed = _clock() - start
            connection.send(("done", (index, elapsed, events)))
            events = []
            if not pending and not final:
                fetch()
        if initialized:
            initialized = False
            adapter.finalize(emit)
        # Preserve uncaptured worker output before terminating the interpreter.
        cleanup_attempted = True
        _finish_worker()
        connection.send(("finished", events))
    except BaseException as error:
        controlled = isinstance(error, WorkerStop)
        failed = not controlled
        detail = str(error) if controlled else traceback.format_exc()
        if controlled and error.completed:
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
        # Pytest has already finalized this session. Do not finalize it again
        # by unwinding the surrounding pytest.main call.
        os._exit(1 if failed else 0)


def execute_parallel(
    adapter,
    indices: list[int],
    *,
    workers: int = 8,
    batch_size: int = 8,
    on_event: Callable[[dict], Any],
    on_complete: Callable[[int, float], None] | None = None,
    groups: list[list[int]] | None = None,
    estimates: Mapping[int, object] | None = None,
    start_worker: Callable | None = None,
    parent_execute: Callable[[int, int | None], None] | None = None,
) -> PoolResult:
    """Run each index once, dynamically assigning pending work to free workers.

    Optional groups are dispatched whole, initially in supplied priority order. Every
    index must belong to exactly one group, in its original collection order.
    Unassigned groups stay shared; only the next group required for pytest's
    fixture lookahead is reserved behind a worker's current group.
    With duration estimates, later-available workers reserve the shortest group
    so expensive work stays available to workers that can start it sooner.

    With parent_execute, workers includes the calling process. All callbacks
    stay on the calling thread while a coordinator handles child IPC. A supplied
    start_worker(worker_id, initial) returns a ready process and its connection;
    pytest uses this to reuse interpreters that collected concurrently.
    """
    if workers < 1 or batch_size < 1:
        raise ValueError("workers and batch_size must be positive")
    if len(set(indices)) != len(indices):
        raise ValueError("job indices must be unique")
    parent_mode = parent_execute is not None
    positions = {index: rank for rank, index in enumerate(indices)}
    child_workers = workers - int(parent_mode)
    pending_groups = None
    weights = {}
    parent_group = deque()
    if groups is not None:
        if any(not group for group in groups):
            raise ValueError("dispatch groups must not be empty")
        flat = [index for group in groups for index in group]
        if len(flat) != len(indices) or set(flat) != set(indices):
            raise ValueError("groups must contain every job index exactly once")
        for group in groups:
            ranks = [positions[index] for index in group]
            if ranks != sorted(ranks):
                raise ValueError("each group must preserve the original indices order")
        weights = estimate_durations(indices, estimates) if estimates else {}
        pending_groups = GroupQueue(groups, weights)
    result = PoolResult()
    if not indices:
        return result
    context = multiprocessing.get_context("spawn")
    pending = deque(indices)
    processes = {}
    receivers = {}
    active = {}
    assigned = {}
    running = {}
    parent_slot = object()
    if parent_mode:
        assigned[parent_slot] = set()
    stopping = False
    state_lock = threading.Lock()
    abort = threading.Event()
    callbacks = SimpleQueue()
    coordinator = None
    coordinator_errors = []

    def deliver(events):
        nonlocal stopping
        for event in events:
            if on_event(event) is False:
                stopping = result.stopped = True

    def worker_events(events):
        if parent_mode:
            callbacks.put(("events", events))
        else:
            deliver(events)

    def worker_completed(index, elapsed):
        if on_complete is not None:
            if parent_mode:
                callbacks.put(("complete", (index, elapsed)))
            else:
                on_complete(index, elapsed)

    def drain_callbacks():
        while True:
            try:
                kind, payload = callbacks.get_nowait()
            except Empty:
                return
            if kind == "events":
                deliver(payload)
            else:
                on_complete(*payload)

    def remaining(owner, now):
        duration = math.fsum(weights[index] for index in assigned[owner])
        if owner in running:
            index, start = running[owner]
            if index in assigned[owner]:
                duration -= min(weights[index], max(0.0, now - start))
        return max(0.0, duration)

    def take_group(owner):
        # The caller holds state_lock so completion and reservation agree.
        shortest = False
        if weights and pending_groups:
            now = _clock()
            ready = remaining(owner, now)
            shortest = any(
                jobs and other is not owner and remaining(other, now) < ready
                for other, jobs in assigned.items()
            )
        return pending_groups.take(shortest=shortest)

    def take_parent_job():
        with state_lock:
            if stopping:
                return None
            if pending_groups is not None:
                if not parent_group and pending_groups:
                    group = take_group(parent_slot)
                    parent_group.extend(group)
                    assigned[parent_slot].update(group)
                return parent_group.popleft() if parent_group else None
            if pending:
                index = pending.popleft()
                assigned[parent_slot].add(index)
                return index
            return None

    def stop_processes(signum):
        signalled = []
        for process in processes.values():
            # The group can outlive its worker leader. In particular a native
            # worker crash must not orphan subprocesses that inherited its group.
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

    def worker_disappeared(connection, detail=None):
        nonlocal stopping
        process = processes[connection]
        process.join(timeout=0.1)
        result.crashes.append(
            {
                "index": active[connection],
                "exitcode": process.exitcode,
                "uncompleted": sorted(assigned[connection]),
                "traceback": detail
                or receivers[connection].incomplete
                or "worker exited without a terminal result",
            }
        )
        del active[connection]
        stopping = True

    def send_batch(connection, batch):
        try:
            connection.send(batch)
        except OSError as error:
            raise TransportError(f"cannot send worker batch: {error}") from error

    def coordinate():
        nonlocal stopping
        while active and not abort.is_set():
            sentinels = {
                processes[connection].sentinel: connection for connection in active
            }
            ready = wait([*active, *sentinels], timeout=0.25)
            # Drain available protocol messages before interpreting a process
            # exit. A normal worker sends its terminal result before exiting.
            for connection in [item for item in ready if item in active]:
                try:
                    for message in receivers[connection].receive():
                        kind, payload = _message_parts(message)
                        if kind == "need":
                            with state_lock:
                                if stopping:
                                    batch = None
                                elif pending_groups is not None:
                                    batch = take_group(connection)
                                elif not pending:
                                    batch = None
                                else:
                                    count = min(
                                        batch_size,
                                        max(1, len(pending) // (len(active) * 2)),
                                    )
                                    batch = [
                                        pending.popleft()
                                        for _ in range(min(count, len(pending)))
                                    ]
                                if batch:
                                    assigned[connection].update(batch)
                            send_batch(connection, batch)
                        elif kind == "started":
                            if payload not in assigned[connection]:
                                raise TransportError(
                                    f"worker started unassigned job {payload}"
                                )
                            active[connection] = payload
                            with state_lock:
                                running[connection] = (payload, _clock())
                        elif kind == "done":
                            index, elapsed, events = payload
                            if (
                                index in result.completed
                                or index not in assigned[connection]
                            ):
                                raise TransportError(
                                    f"worker completed unassigned or repeated job {index}"
                                )
                            with state_lock:
                                result.completed.add(index)
                                assigned[connection].discard(index)
                                running.pop(connection, None)
                            worker_events(events)
                            worker_completed(index, elapsed)
                        elif kind == "finished":
                            if assigned[connection]:
                                raise TransportError(
                                    "worker finished with unaccounted jobs"
                                )
                            worker_events(payload)
                            del active[connection]
                        elif kind == "crash":
                            worker_events(payload.pop("events", []))
                            payload["uncompleted"] = sorted(assigned[connection])
                            result.crashes.append(payload)
                            del active[connection]
                            stopping = True
                        elif kind == "stopped":
                            worker_events(payload.pop("events", []))
                            result.stopped = stopping = True
                            del active[connection]
                        if connection not in active:
                            break
                except TransportError as error:
                    if connection in active:
                        worker_disappeared(
                            connection, f"worker transport error: {error}"
                        )
                if connection in active and receivers[connection].eof:
                    worker_disappeared(connection)
            for sentinel in [item for item in ready if item in sentinels]:
                connection = sentinels[sentinel]
                if connection in active and not connection.poll():
                    # A forked descendant may retain the pipe descriptor, so
                    # worker death cannot rely on receiving pipe EOF.
                    worker_disappeared(connection)
            # multiprocessing's process sentinel is itself an inherited pipe
            # on fork platforms. A grandchild can hold both it and the result
            # pipe open; exitcode polls waitpid and still detects leader death.
            for connection in list(active):
                if processes[connection].exitcode is not None and not connection.poll():
                    worker_disappeared(connection)

    def coordinate_in_thread():
        nonlocal stopping
        try:
            coordinate()
        except BaseException as error:
            coordinator_errors.append(error)
            stopping = True

    def run_parent():
        nonlocal stopping
        index = take_parent_job()
        while index is not None:
            drain_callbacks()
            if stopping:
                break
            next_index = take_parent_job()
            start = _clock()
            with state_lock:
                running[parent_slot] = (index, start)
            complete = True
            try:
                parent_execute(index, next_index)
            except WorkerStop as error:
                complete = error.completed
                stopping = result.stopped = True
            if complete:
                with state_lock:
                    result.completed.add(index)
                    assigned[parent_slot].discard(index)
                    running.pop(parent_slot, None)
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
        available = len(pending_groups) if pending_groups is not None else len(pending)
        for worker_id in range(min(child_workers, available)):
            initial = (
                pending_groups.take()
                if pending_groups is not None
                else [pending.popleft()]
            )
            if start_worker is not None:
                process, parent = start_worker(worker_id, initial)
            else:
                parent, child = context.Pipe(duplex=True)
                process = context.Process(
                    target=_worker,
                    args=(child, worker_id, adapter, initial),
                    name=f"bistra-{worker_id}",
                )
                try:
                    process.start()
                except BaseException:
                    parent.close()
                    raise
                finally:
                    child.close()
            processes[parent] = process
            active[parent] = None
            assigned[parent] = set(initial)
        # Per-call nonblocking reads leave Connection.send unchanged.
        receivers = {connection: FramedReceiver(connection) for connection in active}
        if parent_mode:
            if pending_groups:
                # Give every slot its initial priority group before lookahead.
                initial = pending_groups.take()
                parent_group.extend(initial)
                assigned[parent_slot].update(initial)
            if active:
                coordinator = threading.Thread(
                    target=coordinate_in_thread, name="bistra-coordinator"
                )
                coordinator.start()
            run_parent()
        else:
            coordinate()
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
            # Leaders may already be dead, so join alone gives descendants no
            # opportunity to handle SIGTERM before escalation to SIGKILL.
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

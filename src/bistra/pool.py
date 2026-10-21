import os
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TypeAlias, cast

from .protocol import Bootstrap, Command, TransportError, WorkerMessage, _Channel


@dataclass(frozen=True)
class MessageReceived:
    worker: int
    message: WorkerMessage


@dataclass(frozen=True)
class WorkerExited:
    worker: int
    returncode: int
    error: str | None


PoolEvent: TypeAlias = MessageReceived | WorkerExited


@dataclass
class _Worker:
    process: subprocess.Popen[bytes]
    channel: _Channel
    log: Path
    finished: bool = False
    disconnected: bool = False
    reported: bool = False
    error: str | None = None
    write_error: str | None = None
    failed_at: float | None = None
    exited_at: float | None = None


def _signal_group(pid: int, signum: int) -> bool:
    try:
        os.killpg(pid, signum)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as denied:
        if sys.platform != "darwin":
            raise
        # macOS can reject signals to groups containing only exiting/zombie processes.
        try:
            rows = subprocess.check_output(["ps", "-axo", "pgid=,stat="], text=True, timeout=1)
            members = []
            for row in rows.splitlines():
                group, state = row.split()
                if int(group) == pid:
                    members.append(state)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise denied from None
        if any(not state.startswith("Z") and "E" not in state for state in members):
            raise
        return False


class WorkerPool:
    def __init__(self) -> None:
        self._workers: dict[int, _Worker] = {}
        self._directory = tempfile.TemporaryDirectory(prefix="bistra-startup-")
        self._selector = selectors.DefaultSelector()
        self._closed = False

    def start(self, worker: int, bootstrap: Bootstrap) -> None:
        if self._closed or worker in self._workers:
            raise RuntimeError("worker pool is closed or worker already exists")
        parent, child = socket.socketpair()
        channel = _Channel(parent)
        log = Path(self._directory.name) / f"worker-{worker}.log"
        try:
            channel.queue({**bootstrap, "log": str(log)})
            process = subprocess.Popen(
                [sys.executable, "-m", "bistra.worker", str(child.fileno())],
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(child.fileno(),),
            )
            self._workers[worker] = _Worker(process, channel, log)
            self._selector.register(channel, selectors.EVENT_READ | selectors.EVENT_WRITE, worker)
        except BaseException:
            channel.close()
            raise
        finally:
            child.close()

    def send(self, worker: int, command: Command) -> None:
        child = self._workers[worker]
        if self._closed or child.disconnected or child.reported or child.write_error is not None:
            raise TransportError(f"worker {worker} is disconnected")
        child.channel.queue(command)
        self._selector.modify(child.channel, selectors.EVENT_READ | selectors.EVENT_WRITE, worker)

    def diagnostics(self, worker: int) -> str:
        child = self._workers[worker]
        try:
            with child.log.open("rb") as output:
                output.seek(0, os.SEEK_END)
                output.seek(max(0, output.tell() - 12000))
                return output.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _disconnect(self, child: _Worker, error: str | None) -> None:
        self._selector.unregister(child.channel)
        child.channel.close()
        child.disconnected = True
        if error is not None:
            child.error = error
            child.failed_at = time.monotonic()
            _signal_group(child.process.pid, signal.SIGTERM)

    def _receive(self, worker: int, child: _Worker, events: list[PoolEvent]) -> None:
        try:
            for message in child.channel.receive():
                if message["kind"] in ("bootstrap", "run", "finish", "stop"):
                    raise TransportError(f"unexpected command from worker {worker}: {message['kind']}")
                if message["kind"] == "finished":
                    child.finished = True
                    self._selector.modify(child.channel, selectors.EVENT_READ, worker)
                events.append(MessageReceived(worker, cast(WorkerMessage, message)))
            if child.channel.error is not None:
                raise TransportError(child.channel.error)
            if child.channel.eof:
                error = (
                    None
                    if child.finished
                    else (child.write_error or "worker closed its connection without a final result")
                )
                self._disconnect(child, error)
        except TransportError as error:
            self._disconnect(child, str(error))

    def _flush(self, worker: int, child: _Worker) -> None:
        try:
            child.channel.flush()
        except TransportError as error:
            # A failed write can still leave a final result to read.
            child.write_error = str(error)
            child.failed_at = time.monotonic()
        if not child.channel.pending or child.write_error is not None:
            self._selector.modify(child.channel, selectors.EVENT_READ, worker)

    def poll(self, *, timeout: float) -> list[PoolEvent]:
        if self._closed:
            return []
        events: list[PoolEvent] = []
        for key, mask in self._selector.select(min(max(timeout, 0), 0.1)):
            worker = key.data
            child = self._workers[worker]
            if mask & selectors.EVENT_READ:
                self._receive(worker, child, events)
            if mask & selectors.EVENT_WRITE and not child.disconnected and not child.finished:
                self._flush(worker, child)

        exited: list[tuple[int, _Worker, int]] = []
        now = time.monotonic()
        for worker, child in self._workers.items():
            if child.reported:
                continue
            status = child.process.poll()
            if status is None:
                if child.failed_at is not None and now - child.failed_at >= 0.5:
                    _signal_group(child.process.pid, signal.SIGKILL)
                continue
            if child.exited_at is None:
                child.exited_at = now
            exited.append((worker, child, status))

        readable: set[int] = set()
        if exited:
            readable = {key.data for key, mask in self._selector.select(0) if mask & selectors.EVENT_READ}
        for worker, child, status in exited:
            if not child.disconnected:
                # Drain buffered data before declaring death, but never wait on a
                # descendant that inherited the connection and continues writing.
                if worker in readable and child.exited_at is not None and now - child.exited_at < 0.5:
                    continue
                child.error = child.error or child.channel.incomplete
                self._disconnect(child, None)
            if not child.finished and child.error is None:
                child.error = child.write_error or "worker exited without a final result"
            child.reported = True
            events.append(WorkerExited(worker, status, child.error))
        return events

    def interrupt(self) -> None:
        if self._closed:
            return
        for child in self._workers.values():
            if not child.finished and child.process.poll() is None:
                _signal_group(child.process.pid, signal.SIGINT)

    def close(self) -> None:
        """The controller must drain results before closing the pool."""
        if self._closed:
            return
        self._closed = True
        interrupted = False

        def defer_interrupt(signum: int, frame: FrameType | None) -> None:
            nonlocal interrupted
            interrupted = True

        main_thread = threading.current_thread() is threading.main_thread()
        previous = signal.signal(signal.SIGINT, defer_interrupt) if main_thread else None
        try:
            errors = self._close(lambda: interrupted)
        finally:
            if main_thread:
                signal.signal(signal.SIGINT, previous)
        if interrupted:
            errors.insert(0, KeyboardInterrupt())
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("worker cleanup failed", errors)

    def _close(self, interrupted: Callable[[], bool]) -> list[BaseException]:
        errors: list[BaseException] = []
        groups: set[int] = set()
        for child in self._workers.values():
            try:
                if _signal_group(child.process.pid, signal.SIGTERM):
                    groups.add(child.process.pid)
            except OSError as error:
                errors.append(error)
        deadline = time.monotonic() + 0.5
        while groups and not interrupted() and time.monotonic() < deadline:
            for child in self._workers.values():
                child.process.poll()
            for pid in list(groups):
                try:
                    if not _signal_group(pid, 0):
                        groups.remove(pid)
                except OSError as error:
                    errors.append(error)
                    groups.remove(pid)
            if groups:
                time.sleep(0.01)
        for child in self._workers.values():
            try:
                _signal_group(child.process.pid, signal.SIGKILL)
            except OSError as error:
                errors.append(error)
        deadline = time.monotonic() + 1
        for child in self._workers.values():
            try:
                child.process.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(error)
            try:
                child.channel.close()
            except OSError as error:
                errors.append(error)
        try:
            self._selector.close()
        except OSError as error:
            errors.append(error)
        try:
            self._directory.cleanup()
        except OSError as error:
            errors.append(error)
        return errors

"""Collect tests in fresh interpreters before dispatch."""

import hashlib
import json
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, NoReturn, cast

import pytest

from .executor import _signal_group, _worker
from .transport import FramedReceiver, TransportError

if TYPE_CHECKING:
    from .pytest_adapter import BistraPlugin

pool_key = pytest.StashKey["WorkerPool"]()
child_context: tuple[Connection, int, str, list[int]] | None = None
COLLECTION_TIMEOUT = 120.0


def _manifest(session: pytest.Session) -> str:
    from .pytest_adapter import _fixture_groups

    value = ([item.nodeid for item in session.items], _fixture_groups(session.items))
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _main(
    connection: Connection,
    worker_id: int,
    run_uid: str,
    args: list[str],
    plugins: list[str],
    log_path: str,
) -> None:
    global child_context
    os.setsid()
    original_streams = [os.dup(fd) for fd in (1, 2)]
    with open(log_path, "wb", buffering=0) as output:
        for fd in (1, 2):
            os.dup2(output.fileno(), fd)
    child_context = (connection, worker_id, run_uid, original_streams)
    try:
        status = pytest.main(args, plugins=plugins)
        sys.stdout.flush()
        sys.stderr.flush()
        connection.send(("collection_error", int(status)))
    finally:
        connection.close()


def run_child(session: pytest.Session, owner: "BistraPlugin") -> bool:
    if session.testsfailed:
        # A child with collection errors must not fall back to pytest's serial loop.
        return True
    from .pytest_adapter import PytestAdapter

    assert child_context is not None
    connection, worker_id, _, original_streams = child_context
    connection.send(("ready", _manifest(session)))
    job = connection.recv()
    if job is None:
        # No tests were assigned to this worker.
        return True
    capman = session.config.pluginmanager.get_plugin("capturemanager")
    if capman:
        capman.suspend_global_capture()
    sys.stdout.flush()
    sys.stderr.flush()
    for fd, original in zip((1, 2), original_streams, strict=True):
        os.dup2(original, fd)
        os.close(original)
    adapter = PytestAdapter(session, owner, Path(job["base"]))
    _worker(connection, worker_id, adapter, job["initial"])
    raise AssertionError("Worker loop must terminate its interpreter.")


@dataclass
class _Child:
    process: BaseProcess
    connection: Connection
    log_path: Path
    transferred: bool = False


class WorkerPool:
    def __init__(self) -> None:
        self.children: list[_Child] = []
        self.directory = tempfile.TemporaryDirectory(prefix="bistra-startup-")
        self.base: Path | None = None

    def launch(self, count: int, run_uid: str, args: list[str], plugins: list[str]) -> None:
        context = multiprocessing.get_context("spawn")
        for worker_id in range(count):
            parent, child = context.Pipe(duplex=True)
            log_path = Path(self.directory.name) / f"worker-{worker_id}.log"
            process = context.Process(
                target=_main,
                args=(child, worker_id, run_uid, args, plugins, str(log_path)),
                name=f"bistra-{worker_id}",
            )
            try:
                process.start()
            except BaseException:
                parent.close()
                raise
            finally:
                child.close()
            self.children.append(_Child(process, parent, log_path))

    def _failure(self, child: _Child, message: str) -> NoReturn:
        try:
            with child.log_path.open("rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 12000))
                detail = log.read(12000).decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        raise pytest.UsageError(f"bistra {child.process.name}: {message}\n{detail}")

    def prepare(self, session: pytest.Session, base: Path) -> None:
        expected = _manifest(session)
        pending = {child.connection: child for child in self.children}
        readers = {connection: FramedReceiver(connection) for connection in pending}
        deadline = time.monotonic() + COLLECTION_TIMEOUT
        try:
            while pending:
                for connection in cast(list[Connection], wait(list(pending), timeout=0.1)):
                    child = pending[connection]
                    try:
                        for message in readers[connection].receive():
                            kind, payload = cast(tuple[str, str | int], message)
                            if kind != "ready":
                                self._failure(child, f"collection failed (exit {payload})")
                            if payload != expected:
                                self._failure(
                                    child,
                                    "collection differs from the original process "
                                    "(test order or fixture groups); no tests were executed. "
                                    "Make collection deterministic or use --bistra-workers=1.",
                                )
                            del pending[connection]
                            break
                    except TransportError as error:
                        self._failure(child, f"collection transport failed: {error}")
                for child in pending.values():
                    if child.process.exitcode is not None:
                        self._failure(
                            child,
                            f"exited during collection ({child.process.exitcode})",
                        )
                if pending and time.monotonic() >= deadline:
                    self._failure(
                        next(iter(pending.values())),
                        "collection timed out after the original process finished",
                    )
        finally:
            for reader in readers.values():
                reader.close()
        self.base = base

    def start_worker(self, worker_id: int, initial: list[int]) -> tuple[BaseProcess, Connection]:
        assert self.base is not None, "Collection must be validated before dispatch."
        child = self.children[worker_id]
        child.connection.send({"base": str(self.base), "initial": initial})
        child.transferred = True
        return child.process, child.connection

    def close(self) -> None:
        # Transferred children belong to the executor, which also kills descendants.
        children = [child for child in self.children if not child.transferred]
        for child in children:
            try:
                child.connection.send(None)
            except (OSError, EOFError):
                pass
        for child in children:
            child.process.join(timeout=0.2)
        for signum in (signal.SIGTERM, signal.SIGKILL):
            for child in children:
                assert child.process.pid is not None
                if not _signal_group(child.process.pid, signum) and child.process.is_alive():
                    try:
                        os.kill(child.process.pid, signum)
                    except ProcessLookupError:
                        pass
            for child in children:
                child.process.join(timeout=0.5)
        for child in children:
            child.connection.close()
        self.directory.cleanup()


def _initialize_worker(config: pytest.Config, worker_id: int, run_uid: str) -> None:
    """Set worker identity before importing project configuration."""
    worker_name = f"bistra{worker_id}"
    worker_input = {
        "workerid": worker_name,
        "workercount": config.known_args_namespace.bistra_workers,
        "testrunuid": run_uid,
    }
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    patch.setattr(config, "workerinput", worker_input, raising=False)
    patch.setattr(config, "workeroutput", {}, raising=False)
    patch.setenv("PYTEST_XDIST_WORKER", worker_name)
    patch.setenv("PYTEST_XDIST_WORKER_COUNT", str(worker_input["workercount"]))
    patch.setenv("PYTEST_XDIST_TESTRUNUID", run_uid)


def start_collection(config: pytest.Config) -> None:
    option = config.known_args_namespace
    if (
        not getattr(option, "bistra", False)
        or option.bistra_workers == 1
        or option.collectonly
        or getattr(option, "help", False)
        or getattr(option, "version", False)
    ):
        return
    if child_context is not None:
        _initialize_worker(config, child_context[1], child_context[2])
        return
    if os.name != "posix":
        raise pytest.UsageError("bistra parallel execution currently requires POSIX; use --bistra-workers=1")
    plugins: list[str] = []
    for plugin in config.invocation_params.plugins or ():
        if isinstance(plugin, str):
            plugins.append(plugin)
        elif isinstance(plugin, ModuleType):
            plugins.append(plugin.__name__)
        else:
            raise pytest.UsageError(
                "bistra fresh workers require importable plugins; pass plugin module names "
                "to pytest.main or use --bistra-workers=1"
            )
    pool = WorkerPool()
    config.stash[pool_key] = pool
    config.add_cleanup(pool.close)
    run_uid = os.urandom(16).hex()
    # Pass the original args so pytest expands config and environment addopts only once.
    # Start children before project hooks or the parent change the environment.
    pool.launch(option.bistra_workers - 1, run_uid, list(config.invocation_params.args), plugins)
    _initialize_worker(config, option.bistra_workers - 1, run_uid)

"""Collect tests in fresh interpreters before dispatch."""

import hashlib
import json
import multiprocessing
import os
import pickle
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest

from .executor import _signal_group, _worker
from .lifecycle import WorkerNode
from .protocol import Event
from .transport import FramedReceiver, TransportError

if TYPE_CHECKING:
    from .pytest_adapter import BistraPlugin

pool_key = pytest.StashKey["WorkerPool"]()
COLLECTION_TIMEOUT = 120.0


@dataclass
class ChildContext:
    connection: Connection
    worker_id: int
    worker_input: dict[str, Any]
    original_streams: list[int]
    worker_output: dict[str, Any]
    collection_events: list[Event]


child_context: ChildContext | None = None


@dataclass
class Collection:
    nodeids: list[str]
    groups: list[list[int]]
    events: list[Event]
    deselected: int

    def digest(self) -> str:
        value = (self.nodeids, self.groups)
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _main(
    connection: Connection,
    worker_id: int,
    worker_input: dict[str, Any],
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
    worker_output: dict[str, Any] = {}
    child_context = ChildContext(connection, worker_id, worker_input, original_streams, worker_output, [])
    try:
        status = pytest.main(args, plugins=plugins)
        sys.stdout.flush()
        sys.stderr.flush()
        connection.send(("collection_error", int(status), worker_output, child_context.collection_events))
    finally:
        connection.close()


def run_child(session: pytest.Session, owner: "BistraPlugin") -> bool:
    from .pytest_adapter import PytestAdapter, _fixture_groups

    assert owner.collection_reporter is not None
    reporter = owner.collection_reporter
    session.config.pluginmanager.unregister(reporter)
    assert child_context is not None
    connection, worker_id = child_context.connection, child_context.worker_id
    original_streams = child_context.original_streams
    child_context.collection_events = reporter.events
    if session.testsfailed:
        # A child with collection errors must not fall back to pytest's serial loop.
        return True
    collection = Collection(
        [item.nodeid for item in session.items], _fixture_groups(session.items), [], reporter.deselected
    )
    connection.send(("ready", collection.digest(), collection if worker_id == 0 else None, reporter.events))
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
    node: WorkerNode
    transferred: bool = False


class WorkerPool:
    def __init__(self, config: pytest.Config, plugins: list[str]) -> None:
        self.config = config
        self.plugins = plugins
        self.nodes: list[WorkerNode] = []
        self.closed = False
        self.children: list[_Child] = []
        self.directory = tempfile.TemporaryDirectory(prefix="bistra-startup-")
        self.base: Path | None = None

    def launch(self) -> None:
        count = self.config.option.bistra_workers
        run_uid = os.urandom(16).hex()
        args = list(self.config.invocation_params.args)
        context = multiprocessing.get_context("spawn")
        for worker_id in range(count):
            node = WorkerNode(
                self.config,
                {
                    "workerid": f"bistra{worker_id}",
                    "workercount": count,
                    "testrunuid": run_uid,
                },
            )
            self.nodes.append(node)
            try:
                self.config.hook.pytest_configure_node(node=node)
            except BaseException as error:
                node.error = str(error)
                raise
            parent, child = context.Pipe(duplex=True)
            log_path = Path(self.directory.name) / f"worker-{worker_id}.log"
            process = context.Process(
                target=_main,
                args=(child, worker_id, node.workerinput, args, self.plugins, str(log_path)),
                name=f"bistra-{worker_id}",
            )
            try:
                process.start()
            except BaseException:
                parent.close()
                raise
            finally:
                child.close()
            self.children.append(_Child(process, parent, log_path, node))

    def _failure(self, child: _Child, message: str) -> NoReturn:
        child.node.error = message
        try:
            with child.log_path.open("rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 12000))
                detail = log.read(12000).decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        raise pytest.UsageError(f"bistra {child.process.name}: {message}\n{detail}")

    def collect(self) -> Collection:
        expected: str | None = None
        collection: Collection | None = None
        events: list[Event] = []
        seen: set[bytes] = set()
        pending = {child.connection: child for child in self.children}
        readers = {connection: FramedReceiver(connection) for connection in pending}
        deadline = time.monotonic() + COLLECTION_TIMEOUT
        try:
            while pending:
                for connection in cast(list[Connection], wait(list(pending), timeout=0.1)):
                    child = pending[connection]
                    try:
                        for message in readers[connection].receive():
                            kind, payload, data, worker_events = cast(tuple[Any, ...], message)
                            if kind != "ready":
                                from .pytest_adapter import _replay_collection

                                child.node.workeroutput = data
                                _replay_collection(self.config, worker_events)
                                self._failure(child, f"collection failed (exit {payload})")
                            if expected is None:
                                expected = payload
                            elif payload != expected:
                                self._failure(
                                    child,
                                    "collection differs between workers "
                                    "(test order or fixture groups); no tests were executed. "
                                    "Make collection deterministic or use --bistra-workers=1.",
                                )
                            if data is not None:
                                collection = data
                            # Report shared startup warnings/skips once, while
                            # preserving diagnostics unique to any worker.
                            for event in worker_events:
                                key = pickle.dumps(event)
                                if key not in seen:
                                    seen.add(key)
                                    events.append(event)
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
                        "collection timed out",
                    )
        finally:
            for reader in readers.values():
                reader.close()
        assert collection is not None
        collection.events = events
        return collection

    def start_worker(self, worker_id: int, initial: list[int]) -> tuple[BaseProcess, Connection]:
        assert self.base is not None, "Collection must be validated before dispatch."
        child = self.children[worker_id]
        child.connection.send({"base": str(self.base), "initial": initial})
        child.transferred = True
        return child.process, child.connection

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Transferred children belong to the executor, which also kills descendants.
        children = [child for child in self.children if not child.transferred]
        for child in children:
            try:
                child.connection.send(None)
            except (OSError, EOFError):
                pass
        # Drain shutdown messages while joining: an unused worker can still
        # publish workeroutput from sessionfinish, possibly larger than a pipe.
        readers = {child.connection: FramedReceiver(child.connection) for child in children}
        deadline = time.monotonic() + 2
        try:
            while children and time.monotonic() < deadline:
                for child in children:
                    try:
                        for message in readers[child.connection].receive():
                            payload = cast(tuple[Any, ...], message)
                            if payload[0] == "collection_error":
                                child.node.workeroutput = payload[2]
                    except TransportError:
                        pass
                    child.process.join(timeout=0)
                if all(not child.process.is_alive() for child in children):
                    break
                time.sleep(0.01)
        finally:
            for reader in readers.values():
                reader.close()
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
        for child in self.children:
            if child.process.exitcode and child.node.error is None:
                child.node.error = f"worker exited with status {child.process.exitcode}"
        errors = []
        for node in self.nodes:
            try:
                self.config.hook.pytest_testnodedown(node=node, error=node.error)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise errors[0]


def _initialize_worker(config: pytest.Config, worker_input: dict[str, Any], worker_output: dict[str, Any]) -> None:
    """Set identity and controller-provided resources before project imports."""
    worker_name = worker_input["workerid"]
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    patch.setattr(config, "workerinput", worker_input, raising=False)
    patch.setattr(config, "workeroutput", worker_output, raising=False)
    patch.setenv("PYTEST_XDIST_WORKER", worker_name)
    patch.setenv("PYTEST_XDIST_WORKER_COUNT", str(worker_input["workercount"]))
    patch.setenv("PYTEST_XDIST_TESTRUNUID", worker_input["testrunuid"])


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
        _initialize_worker(config, child_context.worker_input, child_context.worker_output)
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
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        patch.delenv(name, raising=False)
    pool = WorkerPool(config, plugins)
    config.stash[pool_key] = pool
    config.add_cleanup(pool.close)

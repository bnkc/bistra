"""Collect concurrently in fresh interpreters, then hand them to the scheduler."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from .executor import _signal_group, _worker
from .transport import FramedReceiver, TransportError

pool_key = pytest.StashKey()
child_context = None
COLLECTION_TIMEOUT = 120.0


def _manifest(session):
    from .pytest_adapter import _fixture_groups

    value = ([item.nodeid for item in session.items], _fixture_groups(session.items))
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _main(connection, worker_id, run_uid, args, plugins, log_path):
    global child_context
    os.setsid()
    original_streams = [os.dup(fd) for fd in (1, 2)]
    with open(log_path, "wb", buffering=0) as output:
        for fd in (1, 2):
            os.dup2(output.fileno(), fd)
    child_context = (connection, worker_id, run_uid, original_streams)
    from .cli import _PluginLoader

    plugins = [_PluginLoader() if name is None else name for name in plugins]
    try:
        status = pytest.main(args, plugins=plugins)
        sys.stdout.flush()
        sys.stderr.flush()
        connection.send(("collection_error", int(status)))
    finally:
        connection.close()


def run_child(session, owner):
    if session.testsfailed:
        # Never fall through to pytest's serial loop in an unapproved child.
        return True
    from .pytest_adapter import PytestAdapter

    connection, worker_id, _, original_streams = child_context
    connection.send(("ready", _manifest(session)))
    job = connection.recv()
    if job is None:
        # The original process had no work for this interpreter.
        return True
    capman = session.config.pluginmanager.get_plugin("capturemanager")
    if capman:
        capman.suspend_global_capture()
    sys.stdout.flush()
    sys.stderr.flush()
    for fd, original in zip((1, 2), original_streams):
        os.dup2(original, fd)
        os.close(original)
    adapter = PytestAdapter(session, owner, Path(job["base"]))
    _worker(connection, worker_id, adapter, job["initial"])
    raise AssertionError("Worker loop must terminate its interpreter.")


@dataclass
class _Child:
    process: object
    connection: object
    log_path: Path
    transferred: bool = False


class WorkerPool:
    def __init__(self):
        self.children = []
        self.directory = tempfile.TemporaryDirectory(prefix="bistra-startup-")
        self.job_context = None

    def launch(self, count, run_uid, args, plugins):
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

    def _failure(self, child, message):
        try:
            detail = child.log_path.read_text(errors="replace")[-12000:]
        except OSError:
            detail = ""
        raise pytest.UsageError(f"bistra {child.process.name}: {message}\n{detail}")

    def prepare(self, session, base):
        from multiprocessing.connection import wait

        expected = _manifest(session)
        pending = {child.connection: child for child in self.children}
        readers = {connection: FramedReceiver(connection) for connection in pending}
        deadline = time.monotonic() + COLLECTION_TIMEOUT
        try:
            while pending:
                for connection in wait(list(pending), timeout=0.1):
                    child = pending[connection]
                    try:
                        for kind, payload in readers[connection].receive():
                            if kind != "ready":
                                self._failure(
                                    child, f"collection failed (exit {payload})"
                                )
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
                for connection, child in list(pending.items()):
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
        self.job_context = {"base": str(base)}

    def start_worker(self, worker_id, initial):
        child = self.children[worker_id]
        child.connection.send({**self.job_context, "initial": initial})
        child.transferred = True
        return child.process, child.connection

    def close(self):
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
                if (
                    not _signal_group(child.process.pid, signum)
                    and child.process.is_alive()
                ):
                    try:
                        os.kill(child.process.pid, signum)
                    except ProcessLookupError:
                        pass
            for child in children:
                child.process.join(timeout=0.5)
        for child in children:
            child.connection.close()
        self.directory.cleanup()


def _initialize_worker(config, worker_id, run_uid):
    """Publish resource identity before project configuration is imported."""
    worker_input = {
        "workerid": f"bistra{worker_id}",
        "workercount": config.known_args_namespace.bistra_workers,
        "testrunuid": run_uid,
    }
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    patch.setattr(config, "workerinput", worker_input, raising=False)
    patch.setattr(config, "workeroutput", {}, raising=False)
    patch.setenv("PYTEST_XDIST_WORKER", worker_input["workerid"])
    patch.setenv("PYTEST_XDIST_WORKER_COUNT", str(worker_input["workercount"]))
    patch.setenv("PYTEST_XDIST_TESTRUNUID", run_uid)


def start_collection(config):
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
        raise pytest.UsageError(
            "bistra parallel execution currently requires POSIX; use --bistra-workers=1"
        )
    from .cli import _PluginLoader

    plugins = []
    for plugin in config.invocation_params.plugins or ():
        if isinstance(plugin, _PluginLoader):
            plugins.append(None)
        elif isinstance(plugin, str):
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
    # Use original arguments, since pytest expands config and environment addopts.
    # Start children before project hooks or the original execution slot alter the environment.
    pool.launch(
        option.bistra_workers - 1, run_uid, list(config.invocation_params.args), plugins
    )
    _initialize_worker(config, option.bistra_workers - 1, run_uid)

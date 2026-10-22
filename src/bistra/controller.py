import os
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Generator
from dataclasses import dataclass, field
from difflib import unified_diff
from types import FrameType, ModuleType, SimpleNamespace
from typing import Any, assert_never

import pytest

from . import timings
from .pool import MessageReceived, WorkerExited, WorkerPool
from .protocol import Bootstrap, ControlEvent, Event, WorkerMessage
from .reports import replay
from .scheduling import Collection, Scheduler
from .timings import Timing

SHUTDOWN_TIMEOUT = 2.0


@dataclass(frozen=True)
class _Gateway:
    id: str

    def _rinfo(self) -> SimpleNamespace:
        # pytest-cov labels workers through this xdist interface.
        return SimpleNamespace(platform=sys.platform, version_info=tuple(sys.version_info))


@dataclass(eq=False)
class WorkerNode:
    config: pytest.Config
    workerinput: dict[str, Any]
    workeroutput: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    active: bool = False
    ready: bool = False
    requested: bool = False
    finished: bool = False
    status: int | None = None
    collection_events: Counter[tuple[object, ...]] = field(default_factory=Counter)

    @property
    def gateway(self) -> _Gateway:
        return _Gateway(self.workerinput["workerid"])

    @property
    def workerinfo(self) -> dict[str, Any]:
        return {
            "id": self.workerinput["workerid"],
            "version_info": tuple(sys.version_info),
            "sysplatform": sys.platform,
            "executable": sys.executable,
        }

    @property
    def nodemanager(self) -> SimpleNamespace:
        # Local workers need no xdist file synchronization.
        return SimpleNamespace(roots=())


class Controller:
    def __init__(self, config: pytest.Config, *, workers: int) -> None:
        self.config = config
        self.worker_count = workers
        self.parallel = workers > 1 and not config.option.collectonly
        self.pool: WorkerPool | None = None
        self.workers: dict[int, WorkerNode] = {}
        self.session: pytest.Session | None = None
        self.collection: Collection | None = None
        self.scheduler: Scheduler | None = None
        self.measured: dict[str, Timing] = {}
        self.phases: dict[str, dict[str, float]] = {}
        self.collection_events: Counter[tuple[object, ...]] = Counter()
        self.control: ControlEvent | None = None
        self.stopping = False
        self.closed = False
        self.recording = False
        self.shutdown_deadline: float | None = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionstart(self, session: pytest.Session) -> Generator[None, Any, Any]:
        self.session = session
        result = yield
        if self.parallel:
            if threading.current_thread() is threading.main_thread():
                previous = signal.signal(signal.SIGTERM, self._terminate)

                def restore_signal() -> None:
                    signal.signal(signal.SIGTERM, previous)

                self.config.add_cleanup(restore_signal)
            try:
                self._start_workers()
            except BaseException:
                self.close()
                raise
        return result

    def _terminate(self, signum: int, frame: FrameType | None) -> None:
        if self.closed:
            return
        raise pytest.exit.Exception("bistra terminated by SIGTERM", returncode=128 + signum)

    def _start_workers(self) -> None:
        plugins: list[str] = []
        for plugin in self.config.invocation_params.plugins or ():
            if isinstance(plugin, str):
                plugins.append(plugin)
            elif isinstance(plugin, ModuleType):
                plugins.append(plugin.__name__)
            else:
                raise pytest.UsageError("bistra workers require importable plugins; use module names or -j 1")
        self.pool = WorkerPool()
        base = self.config._tmp_path_factory.getbasetemp()  # ty: ignore[unresolved-attribute]
        run_id = os.urandom(16).hex()
        for worker in range(self.worker_count):
            node = WorkerNode(
                self.config,
                {"workerid": f"bistra{worker}", "workercount": self.worker_count, "testrunuid": run_id},
            )
            # Failed provisioning must still receive pytest_testnodedown.
            self.workers[worker] = node
            try:
                self.config.hook.pytest_configure_node(node=node)
                bootstrap: Bootstrap = {
                    "kind": "bootstrap",
                    "args": list(self.config.invocation_params.args),
                    "plugins": plugins,
                    "workerinput": node.workerinput,
                    "path": list(sys.path),
                    "base": str(base / f"worker-{worker}"),
                    "log": "",
                }
                self.pool.start(worker, bootstrap)
                node.active = True
            except BaseException as error:
                node.error = str(error)
                raise

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> bool | None:
        if not self.parallel:
            return None
        self.collection = self._collect()
        session.testscollected = len(self.collection.nodeids)
        return True

    def _collect(self) -> Collection:
        while not all(node.ready for node in self.workers.values()):
            self._poll()
            self._raise_control()
            for worker, node in self.workers.items():
                if node.error or node.finished or not node.active:
                    detail = node.error or "exited before collection completed"
                    raise pytest.exit.Exception(
                        f"bistra worker {worker}: {detail}", returncode=node.status or pytest.ExitCode.INTERNAL_ERROR
                    )
        assert self.collection is not None
        return self.collection

    def _collection_difference(self, worker: int, actual: Collection) -> str:
        assert self.collection is not None
        expected = self.collection
        if expected.nodeids != actual.nodeids:
            difference = "\n".join(
                unified_diff(
                    expected.nodeids, actual.nodeids, fromfile="first worker", tofile=f"worker {worker}", lineterm=""
                )
            )
            return f"bistra worker {worker} collection differs in tests or test order:\n{difference}"

        def groups(values: tuple[tuple[int, ...], ...]) -> list[str]:
            return [repr(tuple(expected.nodeids[index] for index in group)) for group in values]

        for name in ("required_groups", "fixture_groups"):
            before, after = getattr(expected, name), getattr(actual, name)
            if before != after:
                difference = "\n".join(
                    unified_diff(
                        groups(before), groups(after), fromfile="first worker", tofile=f"worker {worker}", lineterm=""
                    )
                )
                return f"bistra worker {worker} collection differs in {name}:\n{difference}"
        raise AssertionError("collections are equal")

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool | None:
        if self.config.option.collectonly:
            return None
        if not self.parallel:
            if session.items and (not session.testsfailed or self.config.option.continue_on_collection_errors):
                self.recording = True
            return None
        assert self.collection is not None
        if self.collection.nodeids:
            self.recording = True
        self.scheduler = Scheduler(self.collection, timings.load(self.config.rootpath), workers=self.worker_count)
        capture = self.config.pluginmanager.get_plugin("capturemanager")
        if capture is not None:
            capture.suspend_global_capture()
        try:
            if session.testsfailed and not self.config.option.continue_on_collection_errors:
                raise session.Interrupted(f"{session.testsfailed} errors during collection")
            self._dispatch()
            self._run()
        finally:
            # Coverage and other wrappers aggregate only after this hook returns.
            self.close()
        self._raise_outcome(session)
        return True

    def _run(self) -> None:
        assert self.pool is not None
        while any(state.active for state in self.workers.values()):
            self._poll()
            if self._should_stop():
                self._stop(
                    cancel=bool(
                        self.control
                        or (self.session and self.session.shouldstop)
                        or any(node.error for node in self.workers.values())
                    )
                )
            else:
                self._dispatch()
            if all(state.finished for state in self.workers.values()) and self.shutdown_deadline is None:
                self.shutdown_deadline = time.monotonic() + SHUTDOWN_TIMEOUT
            if self.shutdown_deadline is not None and time.monotonic() >= self.shutdown_deadline:
                break

    def _should_stop(self) -> bool:
        return bool(
            self.control
            or any(node.error for node in self.workers.values())
            or (self.session and (self.session.shouldfail or self.session.shouldstop))
        )

    def _dispatch(self) -> None:
        if self.scheduler is None or self.stopping or self._should_stop():
            return
        assert self.pool is not None
        for worker, state in self.workers.items():
            if not state.requested:
                continue
            batch = self.scheduler.dispatch(worker, now=time.monotonic())
            self.pool.send(worker, {"kind": "finish"} if batch is None else {"kind": "run", "tests": batch})
            state.requested = False

    def _poll(self) -> None:
        assert self.pool is not None
        for event in self.pool.poll(timeout=0.05):
            if isinstance(event, MessageReceived):
                self._message(event.worker, event.message)
            elif isinstance(event, WorkerExited):
                state = self.workers[event.worker]
                state.active = False
                if self.control or (self.session and self.session.shouldstop):
                    continue
                if event.error or event.returncode or not state.finished:
                    reason = event.error or "worker exited without a final result"
                    if event.returncode:
                        reason = f"{self._exit_description(event.returncode)}: {reason}"
                    index = self.scheduler.current(event.worker) if self.scheduler is not None else None
                    if index is not None and self.collection is not None:
                        reason += f" while running {self.collection.nodeids[index]}"
                    diagnostics = self.pool.diagnostics(event.worker)
                    state.error = state.error or reason + (f"\n{diagnostics}" if diagnostics else "")

    @staticmethod
    def _exit_description(status: int) -> str:
        if status < 0:
            try:
                return f"worker exited from {signal.Signals(-status).name}"
            except ValueError:
                return f"worker exited from signal {-status}"
        return f"worker exited with status {status}"

    def _message(self, worker: int, message: WorkerMessage) -> None:
        state = self.workers[worker]
        if message["kind"] == "ready":
            state.ready = True
            state.requested = True
            if self.collection is None:
                self.collection = message["collection"]
                if message["deselected"]:
                    self._write(f"bistra: {message['deselected']} deselected")
            elif self.collection != message["collection"]:
                raise pytest.UsageError(self._collection_difference(worker, message["collection"]))
            self._events(worker, message["events"], collection=True)
        elif message["kind"] == "need":
            state.requested = True
        elif message["kind"] == "started":
            assert self.scheduler is not None and self.collection is not None
            self.scheduler.started(worker, message["index"], now=time.monotonic())
            self.config.hook.pytest_runtest_logstart(
                nodeid=self.collection.nodeids[message["index"]], location=message["location"]
            )
        elif message["kind"] == "report":
            replay(self.config, {"type": "report", "data": message["data"]}, node=state)
        elif message["kind"] == "completed":
            assert self.scheduler is not None and self.collection is not None
            self._events(worker, message["events"])
            index = message["index"]
            self.scheduler.completed(worker, index)
            nodeid = self.collection.nodeids[index]
            phases = self.phases.pop(nodeid, {})
            self.measured[nodeid] = Timing(
                message["elapsed"], setup=phases.get("setup"), teardown=phases.get("teardown")
            )
        elif message["kind"] == "finished":
            state.finished = True
            state.status = message["status"]
            state.workeroutput = message["workeroutput"]
            self._events(worker, message["events"], collection=not state.ready)
            if message["error"]:
                state.error = message["error"]
            elif not state.ready and not self.control:
                assert self.pool is not None
                diagnostics = self.pool.diagnostics(worker)
                state.error = f"pytest startup failed (exit {message['status']})" + (
                    f"\n{diagnostics}" if diagnostics else ""
                )
            elif message["status"] not in (0, 1, 5) and not self._should_stop():
                state.error = f"pytest worker failed (exit {message['status']})"
            elif self.scheduler is not None and not self._should_stop() and self.scheduler.unfinished(worker):
                state.error = "worker finished with assigned tests remaining"
        else:
            assert_never(message)

    def _events(self, worker: int, events: list[Event], *, collection: bool = False) -> None:
        for event in events:
            key = None
            if collection and event["type"] == "collectreport":
                data = event["data"]
                key = ("collectreport", data["nodeid"], data["outcome"], str(data.get("longrepr")))
            elif collection and event["type"] == "warning":
                data = event["data"]
                key = (
                    "warning",
                    *(data[name] for name in ("module", "qualname", "text", "filename", "lineno", "line")),
                )
            if key is not None:
                counts = self.workers[worker].collection_events
                counts[key] += 1
                # Collapse worker copies, while preserving repeats within one worker.
                if counts[key] <= self.collection_events[key]:
                    continue
                self.collection_events[key] = counts[key]
            if event["type"] == "control":
                if self.stopping:
                    continue
                assert self.session is not None
                if event["kind"] == "fail":
                    self.session.shouldfail = event["message"]
                elif event["kind"] == "stop":
                    self.session.shouldstop = event["message"]
                elif self.control is None:
                    self.control = event
            else:
                replay(self.config, event, node=self.workers[worker])

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when in ("setup", "call", "teardown"):
            phases = self.phases.setdefault(report.nodeid, {})
            phases[report.when] = phases.get(report.when, 0.0) + report.duration

    def _stop(self, *, cancel: bool = False) -> None:
        if cancel and self.shutdown_deadline is None:
            self.shutdown_deadline = time.monotonic() + SHUTDOWN_TIMEOUT
            if self.pool is not None and (
                self.control
                or (self.session and self.session.shouldstop)
                or isinstance(sys.exception(), (KeyboardInterrupt, pytest.exit.Exception))
            ):
                self.pool.interrupt()
        if self.stopping:
            return
        self.stopping = True
        if self.pool is not None:
            for worker, state in self.workers.items():
                if not state.active or state.finished:
                    continue
                try:
                    self.pool.send(worker, {"kind": "stop"})
                except OSError:
                    # The next poll reports a disconnected worker.
                    pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        primary = sys.exception()
        errors: list[BaseException] = []
        if self.pool is not None:
            try:
                self._stop(cancel=True)
                assert self.shutdown_deadline is not None
                while (
                    any(state.active for state in self.workers.values()) and time.monotonic() < self.shutdown_deadline
                ):
                    self._poll()
                for state in self.workers.values():
                    if state.active and not self.control and not (self.session and self.session.shouldstop):
                        state.error = state.error or "worker did not exit during shutdown"
            except BaseException as error:
                errors.append(error)
            finally:
                try:
                    self.pool.close()
                except BaseException as error:
                    errors.append(error)
        for node in self.workers.values():
            if not node.finished and node.error is None and not self._should_stop():
                node.error = "worker did not finish pytest cleanup"
            try:
                self.config.hook.pytest_testnodedown(node=node, error=node.error)
            except BaseException as error:
                errors.append(error)
        if errors:
            if primary is not None:
                for error in errors:
                    primary.add_note(f"Bistra cleanup failed: {error!r}")
            elif len(errors) == 1:
                raise errors[0]
            else:
                raise BaseExceptionGroup("Bistra cleanup failed", errors)

    def _raise_outcome(self, session: pytest.Session) -> None:
        self._raise_control()
        failures = [(worker, node.error) for worker, node in self.workers.items() if node.error]
        if failures:
            for worker, error in failures:
                self._write(f"bistra worker {worker} failure: {error}", red=True)
            assert self.scheduler is not None and self.collection is not None
            incomplete = len(self.collection.nodeids) - self.scheduler.completed_count
            status = next(
                (node.status for node in self.workers.values() if node.error and node.status not in (None, 0, 1, 5)),
                pytest.ExitCode.INTERNAL_ERROR,
            )
            raise pytest.exit.Exception(
                f"bistra workers failed; {incomplete} tests did not complete", returncode=status
            )
        if session.shouldfail:
            raise session.Failed(session.shouldfail)
        if any(node.status == pytest.ExitCode.TESTS_FAILED for node in self.workers.values()):
            session.testsfailed = max(session.testsfailed, 1)
        if session.testscollected and any(
            node.status == pytest.ExitCode.NO_TESTS_COLLECTED for node in self.workers.values()
        ):
            raise pytest.exit.Exception(
                "a pytest worker reported no tests", returncode=pytest.ExitCode.NO_TESTS_COLLECTED
            )

    def _raise_control(self) -> None:
        if self.control is not None:
            if self.control["kind"] == "exit":
                raise pytest.exit.Exception(self.control["message"], self.control.get("returncode"))
            raise KeyboardInterrupt(self.control["message"])
        if self.session and self.session.shouldstop:
            raise self.session.Interrupted(self.session.shouldstop)

    def _write(self, message: str, **style: bool) -> None:
        terminal = self.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(message, **style)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self) -> Generator[None, Any, Any]:
        try:
            self.close()
        finally:
            if self.recording:
                if not self.parallel:
                    self.measured = {
                        nodeid: Timing(sum(phases.values()), setup=phases.get("setup"), teardown=phases.get("teardown"))
                        for nodeid, phases in self.phases.items()
                    }
                try:
                    timings.save(self.config.rootpath, self.measured)
                except OSError:
                    self._write("bistra: could not save test timings", yellow=True)
            result = yield
        return result

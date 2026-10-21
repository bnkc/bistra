import os
import socket
import sys
import time
import traceback
import warnings
from collections import deque
from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager
from typing import Any, Literal, cast

import pytest

from .protocol import Bootstrap, Event, Message, _Channel
from .reports import serialize_warning
from .scheduling import Collection


@contextmanager
def _startup_output(path: str) -> Generator[Callable[[], None], None, None]:
    with ExitStack() as cleanup:
        originals: dict[int, int] = {}
        for fd in (1, 2):
            original = os.dup(fd)
            cleanup.callback(os.close, original)
            originals[fd] = original

        def restore() -> None:
            try:
                for stream in (sys.stdout, sys.stderr):
                    stream.flush()
            finally:
                for fd, original in originals.items():
                    os.dup2(original, fd)

        cleanup.callback(restore)
        with open(path, "wb", buffering=0) as output:
            for fd in (1, 2):
                os.dup2(output.fileno(), fd)
        yield restore


class _TerminalWriter:
    def __init__(self, terminal: Any) -> None:
        self._tw = terminal._tw
        self.write = terminal.write
        self.write_line = terminal.write_line
        self.write_sep = terminal.write_sep


def _fixture_groups(items: Sequence[pytest.Item]) -> tuple[tuple[int, ...], ...]:
    """Dynamic fixture requests are unknown at collection and cannot affect grouping."""
    from _pytest.fixtures import get_scope_node, get_scope_package
    from _pytest.scope import Scope

    owners: set[object] = set()
    groups: dict[object, list[int]] = {}
    for index, item in enumerate(items):
        info = getattr(item, "_fixtureinfo", None)
        if info is None:
            continue
        for name, definitions in info.name2fixturedefs.items():
            if not definitions:
                continue
            definition = definitions[-1]
            callspec = getattr(item, "callspec", None)
            scope = callspec._arg2scope[name] if callspec is not None and name in callspec.params else definition._scope
            if scope is Scope.Function:
                continue
            owner = get_scope_package(item, definition) if scope is Scope.Package else get_scope_node(item, scope)
            if isinstance(owner, pytest.Session):
                if callspec is not None and name in callspec.indices:
                    key = (definition, callspec.indices[name])
                    groups.setdefault(key, []).append(index)
            elif owner is not None:
                owners.add(owner)
    for index, item in enumerate(items):
        for node in item.listchain():
            if node in owners:
                groups.setdefault(node, []).append(index)
    return tuple(tuple(group) for group in groups.values())


def _required_groups(items: Sequence[pytest.Item]) -> tuple[tuple[int, ...], ...]:
    groups: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        names = {
            str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
            for mark in item.iter_markers("xdist_group")
        }
        if names:
            groups.setdefault("_".join(sorted(names)), []).append(index)
    return tuple(tuple(group) for group in groups.values())


class WorkerPlugin:
    def __init__(self, channel: _Channel, bootstrap: Bootstrap, *, restore_output: Callable[[], None]) -> None:
        self.channel = channel
        self.bootstrap = bootstrap
        self.restore_output = restore_output
        self.workeroutput: dict[str, Any] = {}
        self.events: list[Event] = []
        self.pending: deque[int] = deque()
        self.deselected = 0
        self.final = False
        self.stopped = False
        self.config: pytest.Config | None = None
        self.error: str | None = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_cmdline_parse(
        self, pluginmanager: pytest.PytestPluginManager
    ) -> Generator[None, pytest.Config, pytest.Config]:
        config = cast(pytest.Config, pluginmanager.get_plugin("pytestconfig"))
        # Set worker identity before conftest imports and coverage startup.
        config.workerinput = self.bootstrap["workerinput"]  # ty: ignore[unresolved-attribute]
        config.workeroutput = self.workeroutput  # ty: ignore[unresolved-attribute]
        self.config = config
        result = yield
        result.option.xmlpath = None
        return result

    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config: pytest.Config) -> None:
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            config.pluginmanager.unregister(terminal)
            config.pluginmanager.register(_TerminalWriter(terminal), "terminalreporter")

    def _take_events(self) -> list[Event]:
        events, self.events = self.events, []
        return events

    def _accept(self, message: Message) -> None:
        if message["kind"] == "run":
            if not self.stopped:
                self.pending.extend(message["tests"])
        elif message["kind"] == "finish":
            self.final = True
        elif message["kind"] == "stop":
            self.stopped = self.final = True
            self.pending.clear()
        else:
            raise RuntimeError(f"unexpected controller message: {message['kind']}")

    def _describe_collection(self, session: pytest.Session) -> Collection:
        return Collection(
            nodeids=tuple(item.nodeid for item in session.items),
            required_groups=_required_groups(session.items),
            fixture_groups=_fixture_groups(session.items),
        )

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        collection = self._describe_collection(session)
        self.channel.send(
            {
                "kind": "ready",
                "collection": collection,
                "events": self._take_events(),
                "deselected": self.deselected,
            }
        )
        self._accept(self.channel.wait())
        capture = session.config.pluginmanager.get_plugin("capturemanager")
        if capture is not None:
            capture.suspend_global_capture()
        self.restore_output()
        self._run_assigned(session)
        return True

    def _run_assigned(self, session: pytest.Session) -> None:
        while self.pending and not self.stopped:
            for message in self.channel.receive():
                self._accept(message)
            if self.stopped:
                break
            if len(self.pending) == 1 and not self.final:
                # Pytest needs the next item to retain shared fixtures.
                self.channel.send({"kind": "need"})
                self._accept(self.channel.wait())
                for message in self.channel.receive():
                    self._accept(message)
            if not self.pending:
                break
            index = self.pending.popleft()
            item = session.items[index]
            nextitem = session.items[self.pending[0]] if self.pending else None
            self.channel.send({"kind": "started", "index": index, "location": item.location})
            start = time.monotonic()
            session.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
            if session.shouldfail:
                self.events.append({"type": "control", "kind": "fail", "message": str(session.shouldfail)})
            elif session.shouldstop:
                self.events.append({"type": "control", "kind": "stop", "message": str(session.shouldstop)})
            self.channel.send(
                {
                    "kind": "completed",
                    "index": index,
                    "elapsed": time.monotonic() - start,
                    "events": self._take_events(),
                }
            )
            if session.shouldfail:
                raise session.Failed(session.shouldfail)
            if session.shouldstop:
                raise session.Interrupted(session.shouldstop)

    def pytest_keyboard_interrupt(self, excinfo: pytest.ExceptionInfo[BaseException]) -> None:
        error = excinfo.value
        if isinstance(error, pytest.exit.Exception):
            self.events.append(
                {"type": "control", "kind": "exit", "message": error.msg, "returncode": error.returncode}
            )
        elif isinstance(error, pytest.Session.Interrupted):
            self.events.append({"type": "control", "kind": "stop", "message": str(error)})
        else:
            self.events.append({"type": "control", "kind": "interrupt", "message": str(error) or "worker interrupted"})

    def pytest_internalerror(self, excrepr: object) -> bool:
        self.error = str(excrepr)
        return True

    def pytest_runtest_logfinish(self, nodeid: str, location: tuple[str, int | None, str]) -> None:
        self.events.append({"type": "logfinish", "nodeid": nodeid, "location": location})

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        assert self.config is not None
        data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        self.channel.send({"kind": "report", "data": data})

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if not report.passed:
            assert self.config is not None
            data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
            self.events.append({"type": "collectreport", "data": data})

    def pytest_deselected(self, items: Sequence[pytest.Item]) -> None:
        self.deselected += len(items)

    def pytest_warning_recorded(
        self,
        warning_message: warnings.WarningMessage,
        when: Literal["config", "collect", "runtest"],
        nodeid: str,
        location: tuple[str, int, str] | None,
    ) -> None:
        self.events.append(
            {
                "type": "warning",
                "data": serialize_warning(warning_message),
                "when": when,
                "nodeid": nodeid,
                "location": location,
            }
        )


def worker_main(channel: _Channel, bootstrap: Bootstrap) -> int:
    """Publish terminal state only after pytest has finished all cleanup hooks."""
    sys.path[:] = bootstrap["path"]
    workerinput = bootstrap["workerinput"]
    with pytest.MonkeyPatch.context() as environment, _startup_output(bootstrap["log"]) as restore:
        environment.setenv("PYTEST_XDIST_WORKER", workerinput["workerid"])
        environment.setenv("PYTEST_XDIST_WORKER_COUNT", str(workerinput["workercount"]))
        environment.setenv("PYTEST_XDIST_TESTRUNUID", workerinput["testrunuid"])
        plugin = WorkerPlugin(channel, bootstrap, restore_output=restore)
        args = [*bootstrap["args"], f"--basetemp={bootstrap['base']}"]
        error = None
        try:
            status = int(pytest.main(args, plugins=[plugin, *bootstrap["plugins"]]))
        except BaseException:
            status = int(pytest.ExitCode.INTERNAL_ERROR)
            error = traceback.format_exc()
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        channel.send(
            {
                "kind": "finished",
                "status": status,
                "workeroutput": plugin.workeroutput,
                "events": plugin._take_events(),
                "error": error or plugin.error,
            }
        )
        return 1 if error else 0


def _main() -> int:
    channel = _Channel(socket.socket(fileno=int(sys.argv[1])))
    try:
        bootstrap = channel.wait()
        if bootstrap["kind"] != "bootstrap":
            raise RuntimeError("worker expected its bootstrap message")
        return worker_main(channel, bootstrap)
    finally:
        channel.close()


if __name__ == "__main__":
    raise SystemExit(_main())

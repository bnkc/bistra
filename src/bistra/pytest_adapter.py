import hashlib
import os
import signal
import sys
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast

import pytest

from .durations import load_history, save_history, update_history
from .executor import WorkerStop, execute_parallel
from .protocol import ControlEvent, Emit, Event
from .scheduling import estimate_durations

if TYPE_CHECKING:
    from _pytest.capture import CaptureManager
    from _pytest.terminal import TerminalReporter

    from .fresh import Collection, WorkerPool

_UNSUPPORTED = object()
_warning_fallbacks: dict[tuple[str, str, tuple[tuple[str, str], ...]], type[Warning]] = {}


def _warning_value(value, seen=None):
    """Limit warning payloads to values that can cross processes without imports."""
    if type(value) in (type(None), bool, int, float, complex, str, bytes):
        return value
    if type(value) not in (tuple, list, dict):
        return _UNSUPPORTED
    seen = set() if seen is None else seen
    if id(value) in seen:
        return _UNSUPPORTED
    seen.add(id(value))
    try:
        if type(value) is dict:
            converted = [(_warning_value(k, seen), _warning_value(v, seen)) for k, v in value.items()]
            if any(k is _UNSUPPORTED or v is _UNSUPPORTED for k, v in converted):
                return _UNSUPPORTED
            return dict(converted)
        converted = [_warning_value(v, seen) for v in value]
        if any(v is _UNSUPPORTED for v in converted):
            return _UNSUPPORTED
        return tuple(converted) if type(value) is tuple else converted
    finally:
        seen.remove(id(value))


def _warning_class(cls):
    if cls is None:
        return None
    return {
        "module": cls.__module__,
        "qualname": cls.__qualname__,
        "name": cls.__name__,
        "bases": [(base.__module__, base.__qualname__) for base in cls.__mro__[1:] if issubclass(base, Warning)],
    }


def _serialize_warning(warning: warnings.WarningMessage) -> dict[str, Any]:
    message = warning.message
    args = _warning_value(message.args) if isinstance(message, Warning) else _UNSUPPORTED
    attributes = _warning_value(vars(message)) if isinstance(message, Warning) else _UNSUPPORTED
    data = {
        "message_str": str(message),
        "message_class": _warning_class(type(message)) if isinstance(message, Warning) else None,
        "message_args": None if args is _UNSUPPORTED else args,
        "message_args_repr": repr(message.args) if isinstance(message, Warning) else None,
        "message_attributes": {} if attributes is _UNSUPPORTED else attributes,
        "category_class": _warning_class(warning.category),
    }
    for name in warning._WARNING_DETAILS:  # ty: ignore[unresolved-attribute]
        if name not in ("message", "category"):
            value = getattr(warning, name)
            converted = _warning_value(value)
            data[name] = repr(value) if converted is _UNSUPPORTED else converted
    return data


def _resolve_warning_class(module, qualname):
    if "<locals>" in qualname:
        return None
    try:
        # Reporting must not import a worker's test modules into the controller.
        # Unknown classes use the same fallback as local warning classes.
        value = sys.modules.get(module)
        for component in qualname.split("."):
            value = getattr(value, component)
        return value if isinstance(value, type) and issubclass(value, Warning) else None
    except Exception:
        return None


def _reconstruct_warning_class(reference):
    if reference is None:
        return None, False
    cls = _resolve_warning_class(reference["module"], reference["qualname"])
    if cls is not None:
        return cls, False
    key = (reference["module"], reference["qualname"], tuple(reference["bases"]))
    if key not in _warning_fallbacks:
        base = next(
            (
                candidate
                for module, qualname in reference["bases"]
                if (candidate := _resolve_warning_class(module, qualname)) is not None
            ),
            Warning,
        )
        # Local warning classes cannot be imported in the parent. Preserve their
        # name and warning family without changing any imported module.
        # Use Warning's constructor and formatting to avoid custom code.
        try:
            cls = type(
                reference["name"],
                (base,),
                {
                    "__module__": reference["module"],
                    "__qualname__": reference["qualname"],
                    "__new__": staticmethod(Warning.__new__),
                    "__init__": Warning.__init__,
                    "__str__": lambda self: getattr(self, "_bistra_warning_text", Warning.__str__(self)),
                },
            )
        except TypeError:
            cls = type(
                reference["name"],
                (Warning,),
                {
                    "__module__": reference["module"],
                    "__qualname__": reference["qualname"],
                    "__str__": lambda self: getattr(self, "_bistra_warning_text", Warning.__str__(self)),
                },
            )
        _warning_fallbacks[key] = cls
    return _warning_fallbacks[key], True


def _deserialize_warning(data: dict[str, Any]) -> warnings.WarningMessage:
    category, category_fallback = _reconstruct_warning_class(data["category_class"])
    cls, message_fallback = _reconstruct_warning_class(data["message_class"])
    message = data["message_str"]
    if cls is not None:
        if not message_fallback and data["message_args"] is not None:
            try:
                message = cls(*data["message_args"])
                if str(message) != data["message_str"]:
                    message_fallback = True
            except Exception:
                message_fallback = True
        else:
            message_fallback = True
        if message_fallback:
            # Preserve the original text when constructor arguments cannot reproduce it.
            if category_fallback:
                message = category(data["message_str"])
                if data["message_args"] is not None:
                    message.args = data["message_args"]
                message.__dict__.update(data["message_attributes"])
                message._bistra_warning_text = data["message_str"]
            else:
                message = Warning(data["message_str"])
    kwargs = {
        name: data[name]
        for name in warnings.WarningMessage._WARNING_DETAILS  # ty: ignore[unresolved-attribute]
        if name not in ("message", "category")
    }
    warning = warnings.WarningMessage(message, category, **kwargs)
    # Keep qualified class names, text and argument repr for warning hooks to
    # inspect, even when the original warning cannot be reconstructed.
    warning._bistra_warning_metadata = data  # ty: ignore[unresolved-attribute]
    warning._bistra_warning_fallback = category_fallback or message_fallback  # ty: ignore[unresolved-attribute]
    return warning


class _WorkerTerminalProxy:
    """Keep pytest's terminal writer available without worker reporting hooks."""

    def __init__(self, terminal):
        self._terminal = terminal

    def __getattr__(self, name):
        return getattr(self._terminal, name)


class _WorkerReporter:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.emit: Emit | None = None

    def pytest_runtest_logstart(self, nodeid: str, location: tuple[str, int | None, str]) -> None:
        if self.emit:
            self.emit({"type": "logstart", "nodeid": nodeid, "location": location})

    def pytest_runtest_logfinish(self, nodeid: str, location: tuple[str, int | None, str]) -> None:
        if self.emit:
            self.emit({"type": "logfinish", "nodeid": nodeid, "location": location})

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if self.emit:
            data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
            self.emit({"type": "report", "data": data})

    def pytest_warning_recorded(
        self,
        warning_message: warnings.WarningMessage,
        when: Literal["config", "collect", "runtest"],
        nodeid: str,
        location: tuple[str, int, str] | None,
    ) -> None:
        if self.emit:
            self.emit(
                {
                    "type": "warning",
                    "data": _serialize_warning(warning_message),
                    "when": when,
                    "nodeid": nodeid,
                    "location": location,
                }
            )


class _CollectionReporter(_WorkerReporter):
    def __init__(self, config: pytest.Config) -> None:
        super().__init__(config)
        self.events: list[Event] = []
        self.emit = self.events.append
        self.deselected = 0

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if not report.passed:
            data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
            self.events.append({"type": "collectreport", "data": data})

    def pytest_deselected(self, items: Sequence[pytest.Item]) -> None:
        self.deselected += len(items)


def _replay_collection(config: pytest.Config, events: list[Event]) -> None:
    for event in events:
        if event["type"] == "collectreport":
            report = config.hook.pytest_report_from_serializable(config=config, data=event["data"])
            config.hook.pytest_collectreport(report=report)
        elif event["type"] == "warning":
            config.hook.pytest_warning_recorded.call_historic(
                kwargs={
                    "warning_message": _deserialize_warning(event["data"]),
                    "when": event["when"],
                    "nodeid": event["nodeid"],
                    "location": event["location"],
                }
            )


def _initialize_temp_directory(config: pytest.Config, base: Path, worker_id: int) -> None:
    config.option.basetemp = str(base / f"worker-{worker_id}")
    factory = pytest.TempPathFactory.from_config(config, _ispytest=True)
    # Pytest adds these fixture attributes to Config at runtime.
    config._tmp_path_factory = factory  # ty: ignore[unresolved-attribute]
    if hasattr(config, "_tmpdirhandler"):
        config._tmpdirhandler = pytest.TempdirFactory(factory, _ispytest=True)


class PytestAdapter:
    def __init__(self, session: pytest.Session, owner: "BistraPlugin", base: Path) -> None:
        self.session = session
        self.config = session.config
        self.owner = owner
        self.base = base
        self.reporter = _WorkerReporter(self.config)

    def initialize(self, worker_id: int) -> None:
        config = self.config
        pm = config.pluginmanager
        pm.unregister(self.owner)
        terminal = pm.get_plugin("terminalreporter")
        if terminal is not None:
            pm.unregister(terminal)
            pm.register(_WorkerTerminalProxy(terminal), "terminalreporter")
        _initialize_temp_directory(config, self.base, worker_id)
        pm.register(self.reporter, "bistra-worker-reporter")

    def execute(self, index: int, next_index: int | None, emit: Emit) -> None:
        self.reporter.emit = emit
        item = self.session.items[index]
        nextitem = self.session.items[next_index] if next_index is not None else None
        try:
            self.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        except pytest.exit.Exception as error:
            emit(
                {
                    "type": "control",
                    "kind": "exit",
                    "message": error.msg,
                    "returncode": error.returncode,
                }
            )
            raise WorkerStop(error.msg) from None
        except KeyboardInterrupt:
            emit(
                {
                    "type": "control",
                    "kind": "interrupt",
                    "message": "worker interrupted",
                }
            )
            raise WorkerStop("worker interrupted") from None
        if self.session.shouldfail or self.session.shouldstop:
            kind: Literal["fail", "stop"] = "fail" if self.session.shouldfail else "stop"
            reason = str(self.session.shouldfail or self.session.shouldstop)
            emit({"type": "control", "kind": kind, "message": reason})
            raise WorkerStop(reason, completed=True)

    def finalize(self, emit: Emit) -> None:
        self.reporter.emit = emit
        status = pytest.ExitCode.TESTS_FAILED if self.session.testsfailed else pytest.ExitCode.OK
        self.config.hook.pytest_sessionfinish(session=self.session, exitstatus=status)
        output = self.config.workeroutput  # ty: ignore[unresolved-attribute]
        self.config._ensure_unconfigure()
        emit({"type": "workeroutput", "data": output})


def _fixture_groups(items: Sequence[pytest.Item]) -> list[list[int]]:
    """Keep tests that share scoped fixtures together, in collection order.

    Session fixtures stay local to each worker. Group parameterized session
    fixtures by parameter so scheduling does not repeatedly switch values.
    Dynamically requested fixtures use pytest's normal lifecycle but are not
    known during collection, so they cannot affect these groups.
    """
    from _pytest.fixtures import get_scope_node, get_scope_package
    from _pytest.scope import Scope

    roots = list(range(len(items)))

    def root(index):
        while roots[index] != index:
            roots[index] = roots[roots[index]]
            index = roots[index]
        return index

    def join(left, right):
        roots[root(right)] = root(left)

    scoped_owners = set()
    parameters: dict[tuple[object, int], int] = {}
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
                    if key in parameters:
                        join(parameters[key], index)
                    else:
                        parameters[key] = index
                continue
            if owner is not None:
                scoped_owners.add(owner)
    first: dict[object, int] = {}
    for index, item in enumerate(items):
        for node in item.listchain():
            if node in scoped_owners:
                if node in first:
                    join(first[node], index)
                else:
                    first[node] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(items)):
        groups.setdefault(root(index), []).append(index)
    return list(groups.values())


class BistraPlugin:
    def __init__(self, *, workers: int, pool: "WorkerPool | None" = None) -> None:
        self.workers = workers
        self.pool = pool
        self.collection: Collection | None = None
        self.collection_reporter: _CollectionReporter | None = None
        self.phase_durations: dict[str, dict[str, float]] = {}

    @pytest.hookimpl(tryfirst=True)
    def pytest_configure(self, config):
        if (
            getattr(config.option, "numprocesses", None) not in (None, 0)
            or getattr(config.option, "dist", "no") != "no"
            or getattr(config.option, "looponfail", False)
        ):
            raise pytest.UsageError(
                "bistra cannot be combined with xdist worker/distribution options; "
                "use bistra -j or run pytest-xdist separately"
            )
        if self.workers > 1 and (config.option.usepdb or getattr(config.option, "trace", False)):
            raise pytest.UsageError("bistra interactive debugging requires -j 1")
        from .lifecycle import register_hooks

        register_hooks(config)
        from .fresh import child_context

        if child_context is not None:
            self.collection_reporter = _CollectionReporter(config)
            config.pluginmanager.register(self.collection_reporter, "bistra-collection-reporter")

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionstart(self):
        result = yield
        if self.pool is not None:
            try:
                self.pool.launch()
            except BaseException:
                self.pool.close()
                raise
        return result

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self):
        # Resource teardown must happen while controller session resources are
        # still alive, including on collection failure or interruption.
        try:
            if self.pool is not None:
                self.pool.close()
        finally:
            result = yield
        return result

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> bool | None:
        if self.pool is None:
            return None
        # Only workers import test modules and run collection hooks. The
        # controller needs their agreed inventory, not another set of Items.
        self.collection = self.pool.collect()
        session.testscollected = len(self.collection.nodeids)
        _replay_collection(session.config, self.collection.events)
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal and self.collection.deselected:
            terminal.write_line(f"bistra: {self.collection.deselected} deselected")
        return True

    def pytest_report_header(self):
        return f"bistra: fresh interpreters, {self.workers} execution slots"

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when in ("setup", "call", "teardown"):
            phases = self.phase_durations.setdefault(report.nodeid, {})
            phases[report.when] = phases.get(report.when, 0.0) + report.duration

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool | None:
        config = session.config
        from .fresh import child_context, run_child

        if child_context is not None:
            return run_child(session, self)
        if config.option.collectonly:
            return None
        if session.testsfailed:
            if self.workers > 1 and config.option.continue_on_collection_errors:
                raise pytest.UsageError(
                    "bistra requires successful collection before parallel execution; "
                    "use --bistra-workers=1 to continue after collection errors"
                )
            if not config.option.continue_on_collection_errors:
                return None
        if self.workers == 1:
            return None
        assert self.collection is not None
        nodeids = self.collection.nodeids
        if not nodeids:
            return True
        terminal = cast("TerminalReporter | None", config.pluginmanager.get_plugin("terminalreporter"))
        capman = cast("CaptureManager | None", config.pluginmanager.get_plugin("capturemanager"))
        if capman:
            capman.suspend_global_capture()
        base = config._tmp_path_factory.getbasetemp() / "bistra-workers"  # ty: ignore[unresolved-attribute]
        base.mkdir(exist_ok=True)
        key = hashlib.sha256((str(config.rootpath) + sys.version).encode()).hexdigest()[:24]
        cache_path = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "bistra" / f"{key}.json"
        history = load_history(cache_path)
        indices = list(range(len(nodeids)))
        estimates = {index: history[nodeid]["total"] for index, nodeid in enumerate(nodeids) if nodeid in history}
        groups = self.collection.groups
        try:
            estimate_durations(indices, estimates)
        except ValueError:
            # Ignore unusable duration estimates so tests can still run.
            history = {}
            estimates = {}
        measured: dict[str, dict[str, float]] = {}
        worker_exit: ControlEvent | None = None

        def completed(index: int, elapsed: float) -> None:
            nodeid = nodeids[index]
            phases = self.phase_durations.pop(nodeid, {})
            measured[nodeid] = {
                "total": elapsed,
                **{phase: phases.get(phase, 0.0) for phase in ("setup", "call", "teardown")},
            }

        def event(message: Event) -> bool | None:
            nonlocal worker_exit
            assert self.pool is not None
            if message["type"] == "report":
                report = config.hook.pytest_report_from_serializable(config=config, data=message["data"])
                report.node = self.pool.nodes[message["worker_id"]]
                config.hook.pytest_runtest_logreport(report=report)
            elif message["type"] == "logstart":
                config.hook.pytest_runtest_logstart(nodeid=message["nodeid"], location=message["location"])
            elif message["type"] == "logfinish":
                config.hook.pytest_runtest_logfinish(nodeid=message["nodeid"], location=message["location"])
            elif message["type"] == "warning":
                warning = _deserialize_warning(message["data"])
                config.hook.pytest_warning_recorded.call_historic(
                    kwargs={
                        "warning_message": warning,
                        "when": message["when"],
                        "nodeid": message["nodeid"],
                        "location": message["location"],
                    }
                )
            elif message["type"] == "workeroutput":
                self.pool.nodes[message["worker_id"]].workeroutput = message["data"]
            elif message["type"] == "control":
                control = message["kind"]
                if control == "fail":
                    session.shouldfail = message["message"]
                elif control == "stop":
                    session.shouldstop = message["message"]
                elif worker_exit is None:
                    worker_exit = message
                return False
            if session.shouldfail or session.shouldstop:
                return False
            return None

        assert self.pool is not None, "Parallel execution requires collected workers."
        self.pool.base = base
        if terminal:
            terminal.write_line(
                f"bistra: dispatching {len(indices)} tests across {self.workers} fresh worker processes"
            )
            if estimates:
                terminal.write_line(f"bistra: using duration history for {len(estimates)}/{len(indices)} tests")
            else:
                terminal.write_line("bistra: learning test durations")
        result = execute_parallel(
            indices,
            workers=self.workers,
            on_event=event,
            on_complete=completed,
            groups=groups,
            estimates=estimates,
            start_worker=self.pool.start_worker,
        )
        try:
            save_history(cache_path, update_history(history, measured))
        except OSError:
            if terminal:
                terminal.write_line("bistra: could not save optional duration cache", yellow=True)
        if result.crashes:
            for crash in result.crashes:
                if terminal:
                    index = crash.get("index")
                    nodeid = nodeids[index] if index is not None else "during worker startup or cleanup"
                    exitcode = crash.get("exitcode")
                    cause = ""
                    if exitcode is not None:
                        cause = f" ({signal.Signals(-exitcode).name})" if exitcode < 0 else f" (exit {exitcode})"
                    terminal.write_line(
                        f"bistra worker failure: {nodeid}{cause}: " + crash["traceback"],
                        red=True,
                    )
            raise session.Interrupted(
                f"bistra workers failed; {len(indices) - len(result.completed)} tests did not complete"
            )
        if worker_exit is not None:
            if worker_exit["kind"] == "exit":
                # pytest 8's callable protocol loses its first argument in ty.
                # https://github.com/astral-sh/ty/issues/3981
                exit_pytest = cast(Callable[[str, int | None], NoReturn], pytest.exit)
                exit_pytest(worker_exit["message"], worker_exit["returncode"])
            raise KeyboardInterrupt
        if session.shouldfail:
            raise session.Failed(session.shouldfail)
        if session.shouldstop:
            raise session.Interrupted(session.shouldstop)
        return True

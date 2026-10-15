from __future__ import annotations

import hashlib
import importlib
import os
import signal
import sys
import warnings
from pathlib import Path

import pytest

from .durations import load_history, save_history, update_history
from .executor import WorkerStop, execute_parallel
from .scheduling import prioritize_groups

_UNSUPPORTED = object()
_warning_fallbacks = {}


def _warning_value(value, seen=None):
    """Restrict warning payloads to transportable, import-independent values."""
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
            converted = [
                (_warning_value(k, seen), _warning_value(v, seen))
                for k, v in value.items()
            ]
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
        "bases": [
            (base.__module__, base.__qualname__)
            for base in cls.__mro__[1:]
            if issubclass(base, Warning)
        ],
    }


def _serialize_warning(warning):
    message = warning.message
    args = (
        _warning_value(message.args) if isinstance(message, Warning) else _UNSUPPORTED
    )
    data = {
        "message_str": str(message),
        "message_class": _warning_class(type(message))
        if isinstance(message, Warning)
        else None,
        "message_args": None if args is _UNSUPPORTED else args,
        "message_args_repr": repr(message.args)
        if isinstance(message, Warning)
        else None,
        "category_class": _warning_class(warning.category),
    }
    for name in warning._WARNING_DETAILS:
        if name not in ("message", "category"):
            value = getattr(warning, name)
            converted = _warning_value(value)
            data[name] = repr(value) if converted is _UNSUPPORTED else converted
    return data


def _resolve_warning_class(module, qualname):
    if "<locals>" in qualname:
        return None
    try:
        value = importlib.import_module(module)
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
        # Do not mutate imported modules. Local classes have no parent-process
        # identity to recover; preserve their display name and warning family.
        # Warning's construction and formatting avoid inherited custom code.
        try:
            cls = type(
                reference["name"],
                (base,),
                {
                    "__module__": reference["module"],
                    "__qualname__": reference["qualname"],
                    "__new__": staticmethod(Warning.__new__),
                    "__init__": Warning.__init__,
                    "__str__": Warning.__str__,
                },
            )
        except TypeError:
            cls = type(
                reference["name"],
                (Warning,),
                {
                    "__module__": reference["module"],
                    "__qualname__": reference["qualname"],
                },
            )
        _warning_fallbacks[key] = cls
    return _warning_fallbacks[key], True


def _deserialize_warning(data):
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
            # Keep the original text exactly, even if custom formatting cannot
            # be reconstructed from the constructor arguments.
            if category_fallback:
                message = category(data["message_str"])
            else:
                message = Warning(data["message_str"])
    kwargs = {
        name: data[name]
        for name in warnings.WarningMessage._WARNING_DETAILS
        if name not in ("message", "category")
    }
    warning = warnings.WarningMessage(message, category, **kwargs)
    # Observer code can inspect the qualified class names, original text and
    # argument repr even when a local/unconstructable class requires fallback.
    warning._bistra_warning_metadata = data
    warning._bistra_warning_fallback = category_fallback or message_fallback
    return warning


class _WorkerTerminalProxy:
    """Keep pytest's terminal writer available without worker reporting hooks."""

    def __init__(self, terminal):
        self._terminal = terminal

    def __getattr__(self, name):
        return getattr(self._terminal, name)


class _WorkerReporter:
    def __init__(self, config):
        self.config = config
        self.emit = None

    def pytest_runtest_logstart(self, nodeid, location):
        if self.emit:
            self.emit({"type": "logstart", "nodeid": nodeid, "location": location})

    def pytest_runtest_logfinish(self, nodeid, location):
        if self.emit:
            self.emit({"type": "logfinish", "nodeid": nodeid, "location": location})

    def pytest_runtest_logreport(self, report):
        if self.emit:
            data = self.config.hook.pytest_report_to_serializable(
                config=self.config, report=report
            )
            self.emit({"type": "report", "data": data})

    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
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


class PytestAdapter:
    def __init__(self, session, owner, base):
        self.session = session
        self.config = session.config
        self.owner = owner
        self.base = base
        self.reporter = None

    def initialize_temp_directory(self, worker_id):
        from _pytest.tmpdir import TempPathFactory

        self.config.option.basetemp = str(self.base / f"worker-{worker_id}")
        self.config._tmp_path_factory = TempPathFactory.from_config(
            self.config, _ispytest=True
        )
        if hasattr(self.config, "_tmpdirhandler"):
            from _pytest.legacypath import TempdirFactory

            self.config._tmpdirhandler = TempdirFactory(
                self.config._tmp_path_factory, _ispytest=True
            )

    def initialize(self, worker_id):
        config = self.config
        pm = config.pluginmanager
        pm.unregister(self.owner)
        terminal = pm.get_plugin("terminalreporter")
        if terminal is not None:
            pm.unregister(terminal)
            pm.register(_WorkerTerminalProxy(terminal), "terminalreporter")
        from _pytest.junitxml import xml_key

        if xml_key in config.stash:
            xml = config.stash[xml_key]
            pm.unregister(xml)
            del config.stash[xml_key]
        self.initialize_temp_directory(worker_id)
        capman = pm.get_plugin("capturemanager")
        if capman:
            # Fresh interpreters own their capture streams; keep retained log sinks alive.
            capman.suspend_global_capture()
        self.reporter = _WorkerReporter(config)
        pm.register(self.reporter, "bistra-worker-reporter")

    def execute(self, index, next_index, emit):
        from _pytest.outcomes import Exit

        self.reporter.emit = emit
        item = self.session.items[index]
        nextitem = self.session.items[next_index] if next_index is not None else None
        try:
            self.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        except Exit as error:
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
            kind = "fail" if self.session.shouldfail else "stop"
            reason = str(self.session.shouldfail or self.session.shouldstop)
            emit({"type": "control", "kind": kind, "message": reason})
            raise WorkerStop(reason, completed=True)

    def finalize(self, emit):
        self.reporter.emit = emit
        status = (
            pytest.ExitCode.TESTS_FAILED
            if self.session.testsfailed
            else pytest.ExitCode.OK
        )
        self.config.hook.pytest_sessionfinish(session=self.session, exitstatus=status)
        self.config._ensure_unconfigure()


def _fixture_groups(items):
    """Keep shared scoped fixture contexts together in their collected order.

    Session fixtures normally remain per worker. Parameterized session fixtures
    also group their consumers by parameter so priority sorting cannot alternate
    their values repeatedly. Dynamically requested fixtures still follow pytest's
    normal setup and teardown rules but cannot inform grouping before execution.
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
    parameters = {}
    for index, item in enumerate(items):
        info = getattr(item, "_fixtureinfo", None)
        if info is None:
            continue
        for name, definitions in info.name2fixturedefs.items():
            if not definitions:
                continue
            definition = definitions[-1]
            callspec = getattr(item, "callspec", None)
            scope = (
                callspec._arg2scope[name]
                if callspec is not None and name in callspec.params
                else definition._scope
            )
            if scope is Scope.Function:
                continue
            owner = (
                get_scope_package(item, definition)
                if scope is Scope.Package
                else get_scope_node(item, scope)
            )
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
    first = {}
    for index, item in enumerate(items):
        for node in item.listchain():
            if node in scoped_owners:
                if node in first:
                    join(first[node], index)
                else:
                    first[node] = index
    groups = {}
    for index in range(len(items)):
        groups.setdefault(root(index), []).append(index)
    return list(groups.values())


class BistraPlugin:
    def __init__(self, *, workers, pool=None):
        self.workers = workers
        self.pool = pool
        self.phase_durations = {}

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
        if self.workers > 1 and (
            config.option.usepdb or getattr(config.option, "trace", False)
        ):
            raise pytest.UsageError("bistra interactive debugging requires -j 1")
        if self.pool is not None and config.option.xmlpath:
            from _pytest.junitxml import LogXML, xml_key

            # Pytest skips its XML writer on workers; the original slot owns aggregation.
            config.stash[xml_key] = LogXML(
                config.option.xmlpath,
                config.option.junitprefix,
                config.getini("junit_suite_name"),
                config.getini("junit_logging"),
                config.getini("junit_duration_report"),
                config.getini("junit_family"),
                config.getini("junit_log_passing_tests"),
            )
            config.pluginmanager.register(config.stash[xml_key])

    def pytest_report_header(self):
        return f"bistra: fresh interpreters, {self.workers} execution slots"

    def pytest_runtest_logreport(self, report):
        if report.when in ("setup", "call", "teardown"):
            phases = self.phase_durations.setdefault(report.nodeid, {})
            phases[report.when] = phases.get(report.when, 0.0) + report.duration

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
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
        if not session.items:
            return None
        if self.workers == 1:
            return None
        terminal = config.pluginmanager.get_plugin("terminalreporter")
        capman = config.pluginmanager.get_plugin("capturemanager")
        if capman:
            capman.suspend_global_capture()
        base = config._tmp_path_factory.getbasetemp() / "bistra-workers"
        base.mkdir(exist_ok=True)
        key = hashlib.sha256((str(config.rootpath) + sys.version).encode()).hexdigest()[
            :24
        ]
        cache_path = (
            Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
            / "bistra"
            / f"{key}.json"
        )
        history = load_history(cache_path)
        indices = list(range(len(session.items)))
        estimates = {
            index: history[item.nodeid]["total"]
            for index, item in enumerate(session.items)
            if item.nodeid in history
        }
        fixture_groups = _fixture_groups(session.items)
        try:
            groups = (
                prioritize_groups(fixture_groups, estimates)
                if estimates
                else fixture_groups
            )
        except ValueError:
            # Unusable optional estimates must never prevent test execution.
            history = {}
            estimates = {}
            groups = fixture_groups
        measured = {}
        worker_exit = None
        adapter = PytestAdapter(session, self, base)
        parent_initialized = False

        def execute_parent(index, next_index):
            nonlocal parent_initialized
            if not parent_initialized:
                # Collection agreement is checked before fixture initialization.
                # The parent keeps its normal terminal, capture and session hooks.
                adapter.initialize_temp_directory(self.workers - 1)
                parent_initialized = True
            item = session.items[index]
            nextitem = session.items[next_index] if next_index is not None else None
            config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
            if session.shouldfail or session.shouldstop:
                raise WorkerStop(
                    str(session.shouldfail or session.shouldstop), completed=True
                )

        def completed(index, elapsed):
            nodeid = session.items[index].nodeid
            phases = self.phase_durations.pop(nodeid, {})
            measured[nodeid] = {
                "total": elapsed,
                **{
                    phase: phases.get(phase, 0.0)
                    for phase in ("setup", "call", "teardown")
                },
            }

        def event(message):
            nonlocal worker_exit
            kind = message["type"]
            if kind == "report":
                report = config.hook.pytest_report_from_serializable(
                    config=config, data=message["data"]
                )
                config.hook.pytest_runtest_logreport(report=report)
            elif kind == "logstart":
                config.hook.pytest_runtest_logstart(
                    nodeid=message["nodeid"], location=message["location"]
                )
            elif kind == "logfinish":
                config.hook.pytest_runtest_logfinish(
                    nodeid=message["nodeid"], location=message["location"]
                )
            elif kind == "warning":
                warning = _deserialize_warning(message["data"])
                config.hook.pytest_warning_recorded.call_historic(
                    kwargs={
                        "warning_message": warning,
                        "when": message["when"],
                        "nodeid": message["nodeid"],
                        "location": message["location"],
                    }
                )
            elif kind == "control":
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

        self.pool.prepare(session, base)
        if terminal:
            terminal.write_line(
                f"bistra: dispatching {len(indices)} tests across {self.workers} "
                "execution slots (fresh workers + original process)"
            )
            if estimates:
                terminal.write_line(
                    f"bistra: using duration history for {len(estimates)}/{len(indices)} tests"
                )
            else:
                terminal.write_line("bistra: learning test durations")
        result = execute_parallel(
            adapter,
            indices,
            workers=self.workers,
            on_event=event,
            on_complete=completed,
            groups=groups,
            estimates=estimates,
            start_worker=self.pool.start_worker,
            parent_execute=execute_parent,
        )
        try:
            save_history(cache_path, update_history(history, measured))
        except OSError:
            if terminal:
                terminal.write_line(
                    "bistra: could not save optional duration cache", yellow=True
                )
        if result.crashes:
            for crash in result.crashes:
                if terminal:
                    index = crash.get("index")
                    nodeid = (
                        session.items[index].nodeid
                        if index is not None
                        else "during worker startup or cleanup"
                    )
                    exitcode = crash.get("exitcode")
                    cause = ""
                    if exitcode is not None:
                        cause = (
                            f" ({signal.Signals(-exitcode).name})"
                            if exitcode < 0
                            else f" (exit {exitcode})"
                        )
                    terminal.write_line(
                        f"bistra worker failure: {nodeid}{cause}: "
                        + crash["traceback"],
                        red=True,
                    )
            raise session.Interrupted(
                f"bistra workers failed; {len(indices) - len(result.completed)} tests did not complete"
            )
        if worker_exit is not None:
            if worker_exit["kind"] == "exit":
                pytest.exit(
                    worker_exit["message"], returncode=worker_exit["returncode"]
                )
            raise KeyboardInterrupt
        if session.shouldfail:
            raise session.Failed(session.shouldfail)
        if session.shouldstop:
            raise session.Interrupted(session.shouldstop)
        return True

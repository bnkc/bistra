import os
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import pytest

from .cli import parse_worker_count

if TYPE_CHECKING:
    from .controller import WorkerNode


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("bistra", "parallel test execution")
    group.addoption("--bistra", action="store_true", help="run tests in parallel with Bistra")
    group.addoption(
        "--bistra-workers",
        type=parse_worker_count,
        default="auto",
        help="worker processes: auto or a positive integer (default: auto; 1 runs serially)",
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> Generator[None, Any, Any]:
    from .coverage import distributed_coverage

    # Nested runs must not inherit the outer worker's identity.
    if getattr(early_config.known_args_namespace, "bistra", False) and not hasattr(early_config, "workerinput"):
        patch = pytest.MonkeyPatch()
        early_config.add_cleanup(patch.undo)
        for name in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
            patch.delenv(name, raising=False)
    with distributed_coverage(early_config):
        return (yield)


@pytest.fixture(scope="session")
def worker_id(request: pytest.FixtureRequest) -> str:
    return getattr(request.config, "workerinput", {}).get("workerid", "master")


@pytest.fixture(scope="session")
def testrun_uid(request: pytest.FixtureRequest) -> str:
    """An identity shared by all workers in this run."""
    worker = getattr(request.config, "workerinput", None)
    return worker["testrunuid"] if worker is not None else os.urandom(16).hex()


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("bistra") or config.option.help or config.option.version:
        return
    config.addinivalue_line("markers", "xdist_group(name): keep tests with the same group on one worker")
    if (
        getattr(config.option, "numprocesses", None) not in (None, 0)
        or getattr(config.option, "dist", "no") != "no"
        or getattr(config.option, "looponfail", False)
    ):
        raise pytest.UsageError("bistra cannot be combined with xdist worker/distribution options; use bistra -j")
    workers = config.getoption("bistra_workers")
    if workers > 1 and (config.option.usepdb or getattr(config.option, "trace", False)):
        raise pytest.UsageError("bistra interactive debugging requires -j 1")
    if workers > 1 and not config.option.collectonly and os.name != "posix":
        raise pytest.UsageError("bistra parallel execution currently requires POSIX; use --bistra-workers=1")
    _register_hooks(config)
    if hasattr(config, "workerinput"):
        return

    from .controller import Controller

    controller = Controller(config, workers=workers)
    config.add_cleanup(controller.close)
    config.pluginmanager.register(controller, "bistra-controller")


class _Hooks:
    @pytest.hookspec
    def pytest_configure_node(self, node: "WorkerNode") -> None:
        """Provision worker resources before the child imports project code."""

    @pytest.hookspec
    def pytest_testnodedown(self, node: "WorkerNode", error: str | None) -> None:
        """Release resources after the worker and its process group stop."""


def _register_hooks(config: pytest.Config) -> None:
    # xdist may already supply these specifications without controlling this run.
    missing = {
        name: getattr(_Hooks, name)
        for name in ("pytest_configure_node", "pytest_testnodedown")
        if not getattr(config.hook, name, None) or not getattr(config.hook, name).has_spec()
    }
    if missing:
        config.pluginmanager.add_hookspecs(type("BistraLifecycleHooks", (), missing))

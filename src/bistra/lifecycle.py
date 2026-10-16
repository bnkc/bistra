"""Local worker resources, using the lifecycle hooks shared with pytest-xdist."""

import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest


class _Hooks:
    @pytest.hookspec
    def pytest_configure_node(self, node):
        """Populate node.workerinput and provision resources before worker startup."""

    @pytest.hookspec
    def pytest_testnodedown(self, node, error):
        """Release resources after a worker and its descendants have stopped."""


def register_hooks(config: pytest.Config) -> None:
    # xdist may already provide these specs. Register only missing ones, after
    # all initial plugins have loaded, without making xdist a dependency.
    missing = {
        name: getattr(_Hooks, name)
        for name in ("pytest_configure_node", "pytest_testnodedown")
        if not getattr(config.hook, name, None) or not getattr(config.hook, name).has_spec()
    }
    if missing:
        config.pluginmanager.add_hookspecs(type("BistraLifecycleHooks", (), missing))


@dataclass(eq=False)
class WorkerNode:
    config: pytest.Config
    workerinput: dict[str, Any]
    workeroutput: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def workerinfo(self) -> dict[str, Any]:
        # Local spawned workers use the controller's Python interpreter.
        return {
            "id": self.workerinput["workerid"],
            "version_info": tuple(sys.version_info),
            "sysplatform": sys.platform,
            "executable": sys.executable,
        }

    @property
    def gateway(self) -> SimpleNamespace:
        # Common resource hooks use node.gateway.id to name their resources.
        return SimpleNamespace(id=self.workerinput["workerid"])

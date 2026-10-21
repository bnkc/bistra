import sys
from collections.abc import Generator
from contextlib import contextmanager
from importlib import import_module
from typing import Any

import pytest


@contextmanager
def distributed_coverage(early_config: pytest.Config) -> Generator[None, None, None]:
    module = sys.modules.get("pytest_cov.plugin")
    options = early_config.known_args_namespace
    worker = getattr(early_config, "workerinput", None)
    if (
        module is None
        or not early_config.pluginmanager.is_registered(module)
        or not getattr(options, "bistra", False)
        or getattr(options, "bistra_workers", 1) == 1
        or getattr(options, "collectonly", False)
        or getattr(options, "help", False)
        or getattr(options, "version", False)
        or not getattr(options, "cov_source", None)
        or getattr(options, "no_cov", False)
    ):
        yield
        return

    engine = import_module("pytest_cov.engine")
    # pytest-cov has no engine-selection hook. Restore its class before the
    # instance starts, keeping nested pytest invocations independent.
    with pytest.MonkeyPatch.context() as patch:

        class DistributedCovPlugin(module.CovPlugin):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                patch.undo()
                super().__init__(*args, **kwargs)

            def start(self, controller_cls: Any, config: Any = None, nodeid: str | None = None) -> None:
                if controller_cls is engine.Central:
                    if worker is None:
                        controller_cls = engine.DistMaster
                    else:
                        controller_cls = engine.DistWorker
                        config, nodeid = early_config, worker["workerid"]
                # Coverage already includes worker conftest imports. Session
                # startup must not replace that controller and lose them.
                if controller_cls is engine.DistWorker and isinstance(self.cov_controller, engine.DistWorker):
                    return
                super().start(controller_cls, config, nodeid)

        patch.setattr(module, "CovPlugin", DistributedCovPlugin)
        yield

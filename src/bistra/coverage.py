"""Select pytest-cov 5-7's distributed engines without enabling xdist."""

import sys
from collections.abc import Generator
from contextlib import contextmanager
from importlib import import_module

import pytest


@contextmanager
def distributed_coverage(early_config: pytest.Config) -> Generator[None, None, None]:
    from .fresh import pool_key

    module = sys.modules.get("pytest_cov.plugin")
    options = early_config.known_args_namespace
    worker = getattr(early_config, "workerinput", None)
    if (
        module is None
        or not early_config.pluginmanager.is_registered(module)
        or not getattr(options, "bistra", False)
        or (worker is None and early_config.stash.get(pool_key, None) is None)
        or not getattr(options, "cov_source", None)
        or getattr(options, "no_cov", False)
    ):
        yield
        return

    engine = import_module("pytest_cov.engine")

    # pytest-cov has no engine-selection hook. Adapt its plugin once, then
    # leave upstream collection, reporting and thresholds in charge.
    with pytest.MonkeyPatch.context() as patch:

        class DistributedCovPlugin(module.CovPlugin):
            def __init__(self, *args, **kwargs):
                # Restore before initialization or project imports can start
                # another pytest session. Only this instance uses the adapter.
                patch.undo()
                super().__init__(*args, **kwargs)

            def start(self, controller_cls, config=None, nodeid=None):
                if controller_cls is engine.Central:
                    if worker is None:
                        controller_cls = engine.DistMaster
                    else:
                        controller_cls = engine.DistWorker
                        config, nodeid = early_config, worker["workerid"]
                # Keep worker conftest coverage when sessionstart calls again.
                if controller_cls is engine.DistWorker and isinstance(self.cov_controller, engine.DistWorker):
                    return
                super().start(controller_cls, config, nodeid)

        patch.setattr(module, "CovPlugin", DistributedCovPlugin)
        yield

"""Pytest entry point. Installation alone never enables the parallel runner."""

from __future__ import annotations

import argparse
import os

import pytest

from .workers import DEFAULT_WORKERS


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def pytest_addoption(parser):
    group = parser.getgroup("bistra", "parallel test execution")
    group.addoption(
        "--bistra",
        action="store_true",
        help="enable bistra's fresh interpreter workers",
    )
    group.addoption(
        "--bistra-workers",
        type=_positive_int,
        default=DEFAULT_WORKERS,
        help=(
            f"worker processes (default: {DEFAULT_WORKERS}; "
            "1 uses ordinary pytest execution)"
        ),
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    from .fresh import start_collection

    start_collection(early_config)
    return (yield)


@pytest.fixture(scope="session")
def worker_id(request):
    """Expose the execution slot using the established parallel-fixture convention."""
    return getattr(request.config, "workerinput", {}).get("workerid", "master")


@pytest.fixture(scope="session")
def testrun_uid(request):
    """Share a run identity across workers without requiring pytest-xdist."""
    context = getattr(request.config, "workerinput", None)
    return context["testrunuid"] if context is not None else os.urandom(16).hex()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if not config.getoption("bistra"):
        return
    from .fresh import child_context, pool_key
    from .pytest_adapter import BistraPlugin

    if child_context is not None:
        # Only the original process writes the aggregate report.
        config.option.xmlpath = None
    config.pluginmanager.register(
        BistraPlugin(
            workers=config.getoption("bistra_workers"),
            pool=config.stash.get(pool_key, None),
        ),
        name="bistra-engine",
    )

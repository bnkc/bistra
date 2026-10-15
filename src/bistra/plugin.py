"""Pytest hooks and fixtures. Parallel execution requires --bistra."""

import os

import pytest

from .options import DEFAULT_WORKERS, parse_worker_count


def pytest_addoption(parser):
    group = parser.getgroup("bistra", "parallel test execution")
    group.addoption(
        "--bistra",
        action="store_true",
        help="run tests in parallel with Bistra",
    )
    group.addoption(
        "--bistra-workers",
        type=parse_worker_count,
        default=DEFAULT_WORKERS,
        help=(f"worker processes (default: {DEFAULT_WORKERS}; 1 runs serially)"),
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    from .fresh import start_collection

    start_collection(early_config)
    return (yield)


@pytest.fixture(scope="session")
def worker_id(request):
    """Return this worker's ID, or 'master' for serial runs."""
    return getattr(request.config, "workerinput", {}).get("workerid", "master")


@pytest.fixture(scope="session")
def testrun_uid(request):
    """Return a test-run ID shared by all workers."""
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

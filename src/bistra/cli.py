"""Command-line entry point for the opt-in pytest plugin."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .workers import DEFAULT_WORKERS


class _PluginLoader:
    def pytest_addhooks(self, pluginmanager):
        from . import plugin

        # Use the installed entry-point name so subsequent autoload skips it.
        if not pluginmanager.hasplugin("bistra"):
            pluginmanager.register(plugin, name="bistra")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="bistra",
        description="Run pytest in fresh interpreters with automatic duration-aware scheduling.",
        epilog="Pass pytest arguments after --, for example: bistra -j 4 -- -q tests/",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"bistra {__version__}")
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        help=f"worker processes (default: {DEFAULT_WORKERS})",
    )
    if "--" in argv:
        separator = argv.index("--")
        options, forwarded = parser.parse_known_args(argv[:separator])
        forwarded.extend(argv[separator + 1 :])
    else:
        options, forwarded = parser.parse_known_args(argv)
    if options.workers is not None and options.workers < 1:
        parser.error("worker count must be positive")

    pytest_args = ["--bistra"]
    if options.workers is not None:
        pytest_args.extend(("--bistra-workers", str(options.workers)))

    import pytest

    return int(pytest.main([*pytest_args, *forwarded], plugins=[_PluginLoader()]))

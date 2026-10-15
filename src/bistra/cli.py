"""Run pytest with Bistra enabled."""

import argparse
import sys

from . import __version__
from .options import DEFAULT_WORKERS, parse_worker_count


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="bistra",
        description="Run pytest in parallel, using past timings to schedule slower tests first.",
        epilog="Pass pytest arguments after --, for example: bistra -j 4 -- -q tests/",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"bistra {__version__}")
    parser.add_argument(
        "-j",
        "--workers",
        type=parse_worker_count,
        help=f"worker processes (default: {DEFAULT_WORKERS}; 1 runs serially)",
    )
    if "--" in argv:
        separator = argv.index("--")
        options, forwarded = parser.parse_known_args(argv[:separator])
        forwarded.extend(argv[separator + 1 :])
    else:
        options, forwarded = parser.parse_known_args(argv)
    pytest_args = ["-p", "bistra", "--bistra"]
    if options.workers is not None:
        pytest_args.extend(("--bistra-workers", str(options.workers)))

    import pytest

    return int(pytest.main([*pytest_args, *forwarded]))

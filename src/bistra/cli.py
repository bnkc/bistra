import argparse
import os
import sys
from collections.abc import Sequence

from . import __version__


def available_workers() -> int:
    if sys.version_info >= (3, 13):
        count = os.process_cpu_count()
        if count is not None:
            return count

    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0))
        except OSError:
            pass

    return os.cpu_count() or 1


def parse_worker_count(value: str) -> int:
    if value == "auto":
        return available_workers()
    try:
        count = int(value)
    except ValueError:
        count = 0
    if count < 1:
        raise argparse.ArgumentTypeError("must be a positive integer or 'auto'")
    return count


class _HelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=28)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if action.option_strings and action.nargs != 0:
            return f"{', '.join(action.option_strings)} {action.metavar}"
        return super()._format_action_invocation(action)

    def add_usage(self, usage, actions, groups, prefix=None) -> None:
        super().add_usage(usage, actions, groups, prefix="Usage: ")


class _Parser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return "Run your pytest suite with automatic parallel scheduling.\n\n" + super().format_help()


def main(argv: Sequence[str] | None = None) -> int:
    """Help and version exit before importing pytest."""
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _Parser(
        prog="bistra",
        usage="%(prog)s [OPTIONS] [TESTS] [PYTEST_OPTIONS]",
        epilog="Use `bistra -- --help` for all pytest options.",
        formatter_class=_HelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    if sys.version_info >= (3, 14):
        parser.color = False
    options = parser.add_argument_group("Options")
    options.add_argument(
        "-j", "--workers", type=parse_worker_count, metavar="<COUNT>", help="Number of workers [default: auto]"
    )
    options.add_argument("-h", "--help", action="help", help="Show help")
    options.add_argument("-V", "--version", action="version", version=f"bistra {__version__}", help="Show version")
    separator = args.index("--") if "--" in args else len(args)
    parsed, forwarded = parser.parse_known_args(args[:separator])
    forwarded.extend(args[separator + 1 :])
    invocation = ["-p", "bistra", "--bistra"]
    if parsed.workers is not None:
        invocation.extend(("--bistra-workers", str(parsed.workers)))

    import pytest

    return int(pytest.main([*invocation, *forwarded]))

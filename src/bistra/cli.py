"""Run pytest with Bistra enabled."""

import argparse
import sys

from . import __version__
from .options import DEFAULT_WORKERS, parse_worker_count


class _HelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=28)

    def _format_action_invocation(self, action):
        if action.option_strings and action.nargs != 0:
            return f"{', '.join(action.option_strings)} {action.metavar}"
        return super()._format_action_invocation(action)

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix="Usage: ")


class _Parser(argparse.ArgumentParser):
    def format_help(self):
        return "Run your pytest suite with automatic parallel scheduling.\n\n" + super().format_help()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
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
    group = parser.add_argument_group("Options")
    group.add_argument(
        "-j",
        "--workers",
        type=parse_worker_count,
        metavar="<COUNT>",
        help=f"Number of workers [default: {DEFAULT_WORKERS}]",
    )
    group.add_argument("-h", "--help", action="help", help="Show help")
    group.add_argument("-V", "--version", action="version", version=f"bistra {__version__}", help="Show version")
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

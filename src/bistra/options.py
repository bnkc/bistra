"""Shared command-line defaults and validation for Bistra and pytest."""

import argparse

DEFAULT_WORKERS = 12


def parse_worker_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if count < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return count

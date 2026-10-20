"""Shared command-line defaults and validation for Bistra and pytest."""

import argparse
import os

DEFAULT_WORKERS = "auto"


def available_workers() -> int:
    """Use the process CPU count, with fallbacks for older Python versions."""
    process_count = getattr(os, "process_cpu_count", None)
    if process_count is not None:
        count = process_count()
        if count:
            return max(1, count)
    affinity = getattr(os, "sched_getaffinity", None)
    try:
        count = len(affinity(0)) if affinity is not None else 0
    except OSError:
        count = 0
    return max(1, count or os.cpu_count() or 1)


def parse_worker_count(value: str) -> int:
    if value == "auto":
        return available_workers()
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer or 'auto'") from None
    if count < 1:
        raise argparse.ArgumentTypeError("must be a positive integer or 'auto'")
    return count

"""Prioritize expensive test groups while preserving order inside each group."""

from __future__ import annotations

import heapq
import math
from collections import deque
from collections.abc import Mapping, Sequence


def _valid_duration(value: object) -> float | None:
    # Booleans usually indicate corrupt metadata, not a measured duration.
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def estimate_durations(
    indices: Sequence[int], durations: Mapping[int, object]
) -> dict[int, float]:
    """Fill missing estimates using the selected tests' positive mean."""
    known = {index: _valid_duration(durations.get(index)) for index in indices}
    positive = [value for value in known.values() if value is not None and value > 0]
    if positive:
        # Scale before averaging so finite observations cannot overflow the sum.
        scale = max(positive)
        fallback = (
            math.fsum(value / scale for value in positive) / len(positive) * scale
        )
    else:
        fallback = 1.0
    weights = {
        index: fallback if value is None else value for index, value in known.items()
    }
    try:
        total = math.fsum(weights.values())
    except OverflowError as exc:
        raise ValueError("total estimated duration is too large to represent") from exc
    if not math.isfinite(total):
        raise ValueError("total estimated duration is too large to represent")
    return weights


def prioritize_groups(
    groups: Sequence[Sequence[int]], durations: Mapping[int, object]
) -> list[list[int]]:
    """Put expensive indivisible groups first, preserving order within groups.

    Equal-cost groups retain their input order.  Unknown or invalid durations
    use the mean of positive observations for the supplied tests, or 1.0 seconds
    without positive observations.  Zero remains a valid measurement, and stale
    history does not affect the estimate.  Groups must be nonempty and test
    indices must be unique across all groups.
    """
    ordered = [list(group) for group in groups]
    if any(not group for group in ordered):
        raise ValueError("job groups must not be empty")
    indices = [index for group in ordered for index in group]
    if len(set(indices)) != len(indices):
        raise ValueError("job identifiers must be unique")
    weights = estimate_durations(indices, durations)

    costs = [math.fsum(weights[index] for index in group) for group in ordered]
    return [
        ordered[position]
        for position in sorted(
            range(len(ordered)), key=lambda position: -costs[position]
        )
    ]


class GroupQueue:
    """Take whole groups by supplied priority or shortest estimated duration."""

    def __init__(self, groups, estimates):
        self._groups = {rank: list(group) for rank, group in enumerate(groups)}
        self._priority = deque(self._groups)
        self._shortest = (
            [
                (math.fsum(estimates[index] for index in group), rank)
                for rank, group in self._groups.items()
            ]
            if estimates
            else []
        )
        heapq.heapify(self._shortest)

    def __len__(self):
        return len(self._groups)

    def take(self, *, shortest=False):
        if not self._groups:
            return None
        if shortest and self._shortest:
            while True:
                _, rank = heapq.heappop(self._shortest)
                if rank in self._groups:
                    break
        else:
            while True:
                rank = self._priority.popleft()
                if rank in self._groups:
                    break
        return self._groups.pop(rank)

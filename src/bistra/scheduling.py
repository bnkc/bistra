"""Prioritize slower test groups without reordering tests within a group."""

import heapq
import math
from collections import deque
from collections.abc import Mapping, Sequence
from typing import SupportsFloat, SupportsIndex, cast


def _valid_duration(value: object) -> float | None:
    # Reject booleans even though Python treats them as numbers.
    if isinstance(value, bool):
        return None
    try:
        number = float(cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def estimate_durations(indices: Sequence[int], durations: Mapping[int, object]) -> dict[int, float]:
    """Fill missing estimates with the mean of the selected tests' positive timings."""
    known = {index: _valid_duration(durations.get(index)) for index in indices}
    positive = [value for value in known.values() if value is not None and value > 0]
    if positive:
        # Scale before averaging to avoid overflow when adding large durations.
        scale = max(positive)
        fallback = math.fsum(value / scale for value in positive) / len(positive) * scale
    else:
        fallback = 1.0
    weights = {index: fallback if value is None else value for index, value in known.items()}
    try:
        total = math.fsum(weights.values())
    except OverflowError as exc:
        raise ValueError("total estimated duration is too large to represent") from exc
    if not math.isfinite(total):
        raise ValueError("total estimated duration is too large to represent")
    return weights


def prioritize_groups(groups: Sequence[Sequence[int]], durations: Mapping[int, object]) -> list[list[int]]:
    """Put slower groups first without reordering their tests.

    Ties keep their input order. Missing or invalid timings use the mean of
    positive timings from the selected tests, or one second if none are known.
    Zero is valid; history for other tests is ignored. Groups must be nonempty,
    and each test index must appear only once.
    """
    ordered = [list(group) for group in groups]
    if any(not group for group in ordered):
        raise ValueError("job groups must not be empty")
    indices = [index for group in ordered for index in group]
    if len(set(indices)) != len(indices):
        raise ValueError("job identifiers must be unique")
    weights = estimate_durations(indices, durations)

    return sorted(ordered, key=lambda group: -math.fsum(weights[index] for index in group))


class GroupQueue:
    """Take whole groups in priority order or by shortest estimated duration."""

    def __init__(self, groups: Sequence[Sequence[int]], estimates: Mapping[int, float]) -> None:
        self._groups = {rank: list(group) for rank, group in enumerate(groups)}
        self._priority = deque(self._groups)
        self._shortest = (
            [(math.fsum(estimates[index] for index in group), rank) for rank, group in self._groups.items()]
            if estimates
            else []
        )
        heapq.heapify(self._shortest)

    def __len__(self) -> int:
        return len(self._groups)

    def take(self, *, shortest: bool = False) -> list[int] | None:
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

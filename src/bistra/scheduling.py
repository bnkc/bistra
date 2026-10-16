"""Balance fixture groups across workers without changing collection order."""

import heapq
import math
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


def plan_workers(
    indices: Sequence[int],
    groups: Sequence[Sequence[int]],
    workers: int,
    durations: Mapping[int, object],
) -> list[list[list[int]]]:
    """Balance whole fixture groups, then restore each worker's collection order.

    Batches end where affinity groups change. This keeps lookahead accurate while
    allowing the executor to stop between groups instead of buffering a full run.
    """
    if not groups:
        return []
    weights = estimate_durations(indices, durations)
    costs = [math.fsum(weights[index] for index in group) for group in groups]
    slots = [(0.0, 0, worker) for worker in range(min(workers, len(groups)))]
    owners = {}
    for group_id in sorted(range(len(groups)), key=lambda i: -costs[i]):
        total, count, worker = heapq.heappop(slots)
        for index in groups[group_id]:
            owners[index] = (worker, group_id)
        heapq.heappush(slots, (total + costs[group_id], count + len(groups[group_id]), worker))
    batches: list[list[list[int]]] = [[] for _ in slots]
    previous: dict[int, int] = {}
    for index in indices:
        worker, group_id = owners[index]
        if previous.get(worker) != group_id:
            batches[worker].append([])
            previous[worker] = group_id
        batches[worker][-1].append(index)
    return batches

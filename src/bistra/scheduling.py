"""Balance fixture groups across workers without changing collection order."""

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


class WorkScheduler:
    """Rebalance unstarted groups before a worker passes them in collection order.

    Dispatch locks an entire affinity group to its worker, including any later
    batches. Lookahead counts as dispatch: promised next items cannot move.
    """

    def __init__(
        self,
        indices: Sequence[int],
        groups: Sequence[Sequence[int]],
        plans: list[list[list[int]]],
        durations: Mapping[int, object],
    ) -> None:
        self.positions = {index: rank for rank, index in enumerate(indices)}
        self.groups = groups
        self.group_for = {index: group for group, members in enumerate(groups) for index in members}
        self.weights = estimate_durations(indices, durations)
        self.costs = [math.fsum(self.weights[index] for index in group) for group in groups]
        self.queues = [deque(batches) for batches in plans]
        self.queued = [math.fsum(self.weights[i] for batch in queue for i in batch) for queue in self.queues]
        self.pending = [0.0] * len(plans)
        self.running: list[tuple[int, float] | None] = [None] * len(plans)
        self.bounds = [-1] * len(plans)
        self.last_balance = [-math.inf] * len(plans)
        self.locked: set[int] = set()

    def started(self, worker: int, index: int, now: float) -> None:
        self.running[worker] = (index, now)

    def completed(self, worker: int, index: int) -> None:
        self.pending[worker] -= self.weights[index]
        self.running[worker] = None

    def take(self, worker: int, now: float, *, rebalance: bool = True) -> list[int] | None:
        if rebalance:
            self._rebalance(worker, now)
        queue = self.queues[worker]
        if not queue:
            return None
        batch = queue.popleft()
        cost = math.fsum(self.weights[index] for index in batch)
        self.queued[worker] -= cost
        self.pending[worker] += cost
        self.bounds[worker] = self.positions[batch[-1]]
        self.locked.update(self.group_for[index] for index in batch)
        return batch

    def _remaining(self, worker: int, now: float) -> float:
        remaining = self.queued[worker] + self.pending[worker]
        running = self.running[worker]
        if running is not None:
            index, start = running
            remaining -= min(self.weights[index], max(0.0, now - start))
        return max(0.0, remaining)

    def _rebalance(self, worker: int, now: float) -> None:
        # Avoid repeatedly scanning queues for tiny changes or every fast test.
        if len(self.queues) < 2 or now - self.last_balance[worker] < 0.05:
            return
        self.last_balance[worker] = now
        remaining = [self._remaining(slot, now) for slot in range(len(self.queues))]
        donor = max(range(len(self.queues)), key=lambda slot: remaining[slot])
        gap = remaining[donor] - remaining[worker]
        if gap < max(0.1, remaining[worker] * 0.2):
            return
        eligible = {
            self.group_for[batch[0]]
            for batch in self.queues[donor]
            if self.group_for[batch[0]] not in self.locked
            and self.positions[self.groups[self.group_for[batch[0]]][0]] > self.bounds[worker]
        }
        selected: set[int] = set()
        cost = 0.0
        for group in sorted(eligible, key=lambda group: (-self.costs[group], self.positions[self.groups[group][0]])):
            weight = self.costs[group]
            if weight > 0 and cost + weight <= gap / 2:
                selected.add(group)
                cost += weight
        if cost < 0.01:
            return
        moved: list[int] = []
        retained: deque[list[int]] = deque()
        for batch in self.queues[donor]:
            if self.group_for[batch[0]] in selected:
                moved.extend(batch)
            else:
                retained.append(batch)
        self.queues[donor] = retained
        indices = sorted([*moved, *(i for batch in self.queues[worker] for i in batch)], key=self.positions.__getitem__)
        merged: deque[list[int]] = deque()
        previous = None
        for index in indices:
            group = self.group_for[index]
            if previous != group:
                merged.append([])
                previous = group
            merged[-1].append(index)
        self.queues[worker] = merged
        self.queued[donor] -= cost
        self.queued[worker] += cost

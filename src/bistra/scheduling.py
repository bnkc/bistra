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


def refine_fixture_groups(
    indices: Sequence[int],
    groups: Sequence[Sequence[int]],
    required: Sequence[Sequence[int]],
    workers: int,
    history: Mapping[int, Mapping[str, float]],
) -> list[list[int]]:
    """Trade ordinary fixture reuse for parallelism when history makes it cheap.

    Required groups are indivisible. Ordinary scoped fixtures may have one
    instance per worker, just as session fixtures do. Without complete timing
    history, keep the original locality. For each extra replica, charge the
    *entire* group's measured setup and teardown, including function fixtures:
    a deliberately conservative estimate, without guessing fixture identities.
    """
    roots = {index: index for index in indices}

    def root(index: int) -> int:
        while roots[index] != index:
            roots[index] = roots[roots[index]]
            index = roots[index]
        return index

    def combine(collections: Sequence[Sequence[int]]) -> list[list[int]]:
        for members in collections:
            for index in members[1:]:
                roots[root(index)] = root(members[0])
        combined: dict[int, list[int]] = {}
        for index in indices:
            combined.setdefault(root(index), []).append(index)
        return list(combined.values())

    # Explicit groups may cross fixture boundaries. Merge those boundaries for
    # locality, but retain the explicit groups themselves as indivisible atoms.
    atoms = combine(required)
    atom_for = {index: atom for atom in atoms for index in atom}
    locality = combine(groups)
    weights = estimate_durations(indices, {i: record["total"] for i, record in history.items() if "total" in record})
    # Replication cannot beat the suite's average worker load or its longest
    # indivisible group. Below that floor, extra fixtures buy no parallelism.
    floor = max(
        math.fsum(weights.values()) / max(workers, 1),
        max((math.fsum(weights[i] for i in atom) for atom in atoms), default=0.0),
    )
    result: list[list[int]] = []
    for members in locality:
        parts = [atom_for[index] for index in members if atom_for[index][0] == index]
        if workers < 2 or len(parts) < 2:
            result.append(members)
            continue
        best = [members]
        cost = math.fsum(weights[index] for index in members)
        if cost <= floor:
            result.append(members)
            continue
        phases = [
            _valid_duration(history.get(index, {}).get(phase))
            for index in members
            for phase in ("total", "setup", "teardown")
        ]
        if all(value is not None for value in phases):
            # Setup/teardown are an upper estimate of the reusable portion. Do
            # not subtract them from test weights; the original instance still
            # has to run. Saturate implausibly large phase totals at group cost.
            overhead = 0.0
            for index in members:
                overhead = min(cost, overhead + history[index]["setup"] + history[index]["teardown"])
            best_cost = cost
            largest = max(math.fsum(weights[i] for i in part) for part in parts)
            for count in range(2, min(workers, len(parts)) + 1):
                if max(floor, largest, cost / count) + (count - 1) * overhead >= best_cost:
                    continue
                plans = plan_workers(members, parts, count, weights)
                chunks = [[i for batch in plan for i in batch] for plan in plans]
                longest = max(math.fsum(weights[i] for i in chunk) for chunk in chunks)
                projected = max(floor, longest) + (count - 1) * overhead
                if projected < best_cost:
                    best, best_cost = chunks, projected
        result.extend(best)
    positions = {index: position for position, index in enumerate(indices)}
    return sorted(result, key=lambda members: positions[members[0]])


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
        # The busiest worker may have no transferable groups. Try other donors
        # before giving up, keeping the same ownership and ordering constraints.
        for donor in sorted(range(len(self.queues)), key=lambda slot: -remaining[slot]):
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
            for group in sorted(
                eligible, key=lambda group: (-self.costs[group], self.positions[self.groups[group][0]])
            ):
                weight = self.costs[group]
                if weight > 0 and cost + weight <= gap / 2:
                    selected.add(group)
                    cost += weight
            if cost < 0.01:
                continue
            moved: list[int] = []
            retained: deque[list[int]] = deque()
            for batch in self.queues[donor]:
                if self.group_for[batch[0]] in selected:
                    moved.extend(batch)
                else:
                    retained.append(batch)
            self.queues[donor] = retained
            indices = sorted(
                [*moved, *(i for batch in self.queues[worker] for i in batch)], key=self.positions.__getitem__
            )
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
            return

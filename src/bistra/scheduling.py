import heapq
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import groupby
from typing import TypeAlias

from .timings import Timing

Batch: TypeAlias = tuple[int, ...]
_REBALANCE_INTERVAL = 0.05
_MIN_TRANSFER_SECONDS = 0.01


@dataclass(frozen=True)
class Collection:
    """Groups may overlap. Required groups stay together; fixture groups may split.

    Splitting must justify the extra fixture setup and teardown.
    """

    nodeids: tuple[str, ...]
    required_groups: tuple[Batch, ...]
    fixture_groups: tuple[Batch, ...]


@dataclass
class _Slot:
    queued: deque[Batch]
    queued_seconds: float
    pending: deque[int] = field(default_factory=deque)
    pending_seconds: float = 0.0
    running: tuple[int, float] | None = None
    last_dispatched: int = -1
    last_balance: float = -math.inf
    exhausted: bool = False


class Scheduler:
    """Preserve collection order within each worker.

    Dispatch fixes a whole group's worker, including lookahead. Only
    undispatched groups can move. All timestamps are monotonic seconds.
    """

    def __init__(self, collection: Collection, history: Mapping[str, Timing], *, workers: int) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        try:
            self._weights = _estimates(collection.nodeids, history)
            self._groups, self._weights = _plan(collection, history, self._weights, workers)
            math.fsum(self._weights)
        except (OverflowError, ValueError):
            # Individually finite history can still overflow when summed.
            self._weights = (1.0,) * len(collection.nodeids)
            self._groups, self._weights = _plan(collection, {}, self._weights, workers)
        self._group_for = {index: group for group, members in enumerate(self._groups) for index in members}
        self._costs = tuple(math.fsum(self._weights[index] for index in group) for group in self._groups)
        self._slots = [
            _Slot(_batches(indices, self._group_for), math.fsum(self._weights[index] for index in indices))
            for indices in _assign(self._groups, self._weights, workers)
        ]
        self._locked: set[int] = set()
        self._completed_count = 0

    @property
    def completed_count(self) -> int:
        return self._completed_count

    def current(self, worker: int) -> int | None:
        running = self._slots[worker].running
        return None if running is None else running[0]

    def unfinished(self, worker: int) -> tuple[int, ...]:
        slot = self._slots[worker]
        return (*slot.pending, *(index for batch in slot.queued for index in batch))

    def dispatch(self, worker: int, *, now: float) -> Batch | None:
        slot = self._slots[worker]
        if slot.exhausted:
            return None
        if slot.last_dispatched >= 0:
            self._rebalance(worker, now)
        if not slot.queued:
            slot.exhausted = True
            return None
        batch = slot.queued.popleft()
        cost = math.fsum(self._weights[index] for index in batch)
        slot.queued_seconds -= cost
        slot.pending_seconds += cost
        slot.pending.extend(batch)
        slot.last_dispatched = batch[-1]
        self._locked.add(self._group_for[batch[0]])
        return batch

    def started(self, worker: int, index: int, *, now: float) -> None:
        slot = self._slots[worker]
        if slot.running is not None or not slot.pending or slot.pending[0] != index:
            raise ValueError(f"worker {worker} started unexpected test {index}")
        slot.running = (index, now)

    def completed(self, worker: int, index: int) -> None:
        slot = self._slots[worker]
        if slot.running is None or slot.running[0] != index:
            raise ValueError(f"worker {worker} completed unexpected test {index}")
        slot.pending.popleft()
        slot.pending_seconds -= self._weights[index]
        slot.running = None
        self._completed_count += 1

    def _remaining(self, slot: _Slot, now: float) -> float:
        remaining = slot.queued_seconds + slot.pending_seconds
        if slot.running is not None:
            index, start = slot.running
            remaining -= min(self._weights[index], max(0.0, now - start))
        return max(0.0, remaining)

    def _rebalance(self, worker: int, now: float) -> None:
        slot = self._slots[worker]
        # Scanning on every fast test costs more than small transfers save.
        if len(self._slots) < 2 or now - slot.last_balance < _REBALANCE_INTERVAL:
            return
        slot.last_balance = now
        remaining = [self._remaining(other, now) for other in self._slots]
        for donor in sorted(range(len(self._slots)), key=lambda candidate: -remaining[candidate]):
            gap = remaining[donor] - remaining[worker]
            if gap < 2 * _MIN_TRANSFER_SECONDS:
                return
            eligible: set[int] = set()
            for batch in self._slots[donor].queued:
                group = self._group_for[batch[0]]
                if group not in self._locked and self._groups[group][0] > slot.last_dispatched:
                    eligible.add(group)
            selected: set[int] = set()
            cost = 0.0
            for group in sorted(eligible, key=lambda group: (-self._costs[group], self._groups[group][0])):
                weight = self._costs[group]
                if weight > 0 and cost + weight <= gap / 2:
                    selected.add(group)
                    cost += weight
            if cost >= _MIN_TRANSFER_SECONDS:
                self._transfer(donor, worker, selected, cost)
                return

    def _transfer(self, donor: int, recipient: int, groups: set[int], cost: float) -> None:
        source, target = self._slots[donor], self._slots[recipient]
        retained: deque[Batch] = deque()
        indices = [index for batch in target.queued for index in batch]
        for batch in source.queued:
            if self._group_for[batch[0]] in groups:
                indices.extend(batch)
            else:
                retained.append(batch)
        source.queued = retained
        target.queued = _batches(sorted(indices), self._group_for)
        source.queued_seconds -= cost
        target.queued_seconds += cost


def _estimates(nodeids: Sequence[str], history: Mapping[str, Timing]) -> tuple[float, ...]:
    known = [history[nodeid].total if nodeid in history else None for nodeid in nodeids]
    positive = [value for value in known if value is not None and value > 0]
    fallback = 1.0
    if positive:
        scale = max(positive)
        fallback = math.fsum(value / scale for value in positive) / len(positive) * scale
    weights = tuple(fallback if value is None else value for value in known)
    if not math.isfinite(math.fsum(weights)):
        raise ValueError("total estimated duration is not finite")
    return weights


def _merge_groups(count: int, groups: Sequence[Batch]) -> tuple[Batch, ...]:
    roots = list(range(count))

    def root(index: int) -> int:
        while roots[index] != index:
            roots[index] = roots[roots[index]]
            index = roots[index]
        return index

    for group in groups:
        for index in group[1:]:
            roots[root(index)] = root(group[0])
    combined: dict[int, list[int]] = {}
    for index in range(count):
        combined.setdefault(root(index), []).append(index)
    return tuple(tuple(group) for group in combined.values())


def _plan(
    collection: Collection, history: Mapping[str, Timing], weights: Sequence[float], workers: int
) -> tuple[tuple[Batch, ...], tuple[float, ...]]:
    """Estimate shared fixture work as phase time above each group's minimum.

    The minimum is a per-test baseline. Excess may include slow individual
    setups, so this deliberately coarse estimate can overstate shared work.
    """
    atoms = _merge_groups(len(weights), collection.required_groups)
    atom_for = {index: atom for atom in atoms for index in atom}
    locality = _merge_groups(len(weights), (*atoms, *collection.fixture_groups))
    # Splitting below this floor cannot improve the suite's expected finish.
    total = math.fsum(weights)
    average = total / workers
    floor = max(
        average,
        max((math.fsum(weights[index] for index in atom) for atom in atoms), default=0.0),
    )
    result: list[Batch] = []
    planned_weights = list(weights)
    for members in locality:
        parts = [atom_for[index] for index in members if atom_for[index][0] == index]
        cost = math.fsum(weights[index] for index in members)
        if workers == 1 or len(parts) == 1 or cost <= floor:
            result.append(members)
            continue
        records = [history.get(collection.nodeids[index]) for index in members]
        phases = [
            (record.setup, record.teardown)
            for record in records
            if record is not None and record.setup is not None and record.teardown is not None
        ]
        if len(phases) != len(members):
            result.append(members)
            continue
        baseline_setup = min(setup for setup, _ in phases)
        baseline_teardown = min(teardown for _, teardown in phases)
        per_test = {index: weights[index] for index in members}
        shared_parts = []
        for index, (setup, teardown) in zip(members, phases, strict=True):
            shared = min(weights[index], setup - baseline_setup + teardown - baseline_teardown)
            shared_parts.append(shared)
            per_test[index] -= shared
        shared = math.fsum(shared_parts)
        # Reserve a proportional share of workers for the rest of the suite.
        count = min(workers, len(parts), int(cost / total * workers))
        if count < 2:
            result.append(members)
            continue
        chunks = _assign(parts, per_test, count)
        longest = max(math.fsum(per_test[index] for index in chunk) for chunk in chunks)
        # Each worker pays shared setup concurrently, once on its own path.
        if max(floor, shared + longest) >= cost:
            result.append(members)
            continue
        for index in members:
            planned_weights[index] = per_test[index]
        for chunk in chunks:
            planned_weights[chunk[0]] += shared
        result.extend(chunks)
    groups = tuple(sorted(result, key=lambda group: group[0]))
    if groups != locality and _makespan(groups, planned_weights, workers) < _makespan(locality, weights, workers):
        return groups, tuple(planned_weights)
    return locality, tuple(weights)


def _makespan(groups: Sequence[Batch], weights: Sequence[float], workers: int) -> float:
    return max(math.fsum(weights[index] for index in indices) for indices in _assign(groups, weights, workers))


def _assign(groups: Sequence[Batch], weights: Sequence[float] | Mapping[int, float], workers: int) -> list[Batch]:
    costs = [math.fsum(weights[index] for index in group) for group in groups]
    slots = [(0.0, 0, worker) for worker in range(workers)]
    assigned: list[list[int]] = [[] for _ in slots]
    for group in sorted(range(len(groups)), key=lambda group: -costs[group]):
        total, count, worker = heapq.heappop(slots)
        assigned[worker].extend(groups[group])
        heapq.heappush(slots, (total + costs[group], count + len(groups[group]), worker))
    return [tuple(sorted(indices)) for indices in assigned]


def _batches(indices: Sequence[int], group_for: Mapping[int, int]) -> deque[Batch]:
    return deque(tuple(members) for _, members in groupby(indices, key=group_for.__getitem__))

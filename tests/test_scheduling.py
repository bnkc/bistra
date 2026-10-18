import heapq
import math
import random
from collections import deque

import pytest

from bistra.scheduling import WorkScheduler, estimate_durations, plan_workers


def test_balances_expensive_tests_without_reversing_execution():
    groups = [[0], [1], [2], [3]]
    plans = plan_workers([0, 1, 2, 3], groups, 2, {0: 1, 1: 2, 2: 6, 3: 5})
    assert plans == [[[0], [2]], [[1], [3]]]
    assert groups == [[0], [1], [2], [3]]


def test_interleaved_affinity_groups_preserve_collection_order_and_batch_boundaries():
    indices = [7, 4, 9, 2, 6, 1]
    groups = [[7, 9], [4], [2, 6], [1]]
    plans = plan_workers(indices, groups, 2, {7: 2, 9: 2, 4: 1, 2: 4, 6: 4, 1: 1})
    assert plans == [[[2, 6]], [[7], [4], [9], [1]]]
    owners = {index: worker for worker, batches in enumerate(plans) for batch in batches for index in batch}
    assert set(owners) == set(indices)
    for group in groups:
        assert len({owners[index] for index in group}) == 1


def test_unknown_durations_use_selected_positive_mean():
    durations = {0: "2", 1: 4, 2: 0, 3: -1, 4: math.nan, 5: math.inf, 6: "bad", 7: True, 99: 10000}
    assert estimate_durations(list(range(9)), durations) == {0: 2, 1: 4, 2: 0, **dict.fromkeys(range(3, 9), 3)}
    assert estimate_durations([0, 1], {0: 0, 99: 10000}) == {0: 0, 1: 1}


@pytest.mark.parametrize("durations", [{}, dict.fromkeys(range(6), 0)])
def test_cold_and_zero_duration_plans_use_all_workers(durations):
    plans = plan_workers(list(range(6)), [[i] for i in range(6)], 3, durations)
    assert plans == [[[0], [3]], [[1], [4]], [[2], [5]]]


def test_worker_count_is_bounded_by_affinity_groups():
    assert plan_workers([0, 1, 2], [[0, 1], [2]], 8, {}) == [[[0, 1]], [[2]]]
    assert plan_workers([], [], 8, {}) == []


def test_unrepresentable_total_is_rejected():
    with pytest.raises(ValueError, match="too large"):
        estimate_durations([0, 1], {0: 1e308, 1: 1e308})


def test_early_finisher_can_help_before_passing_unstarted_groups():
    groups = [[0], [1], [2, 4], [3, 5], [6], [7], [8], [9]]
    plans = [[[0], [2, 4], [6], [8]], [[1], [3, 5], [7], [9]]]
    scheduler = WorkScheduler(list(range(10)), groups, plans, {**dict.fromkeys(range(10), 1), 0: 8, 1: 8})
    seen = [[*scheduler.take(0, 0, rebalance=False)], [*scheduler.take(1, 0, rebalance=False)]]
    scheduler.started(0, 0, 0)
    scheduler.started(1, 1, 0)
    scheduler.completed(0, 0)
    # The first worker finishes much sooner than its history predicted. It can
    # help now; waiting until its own queue ends would pass all eligible work.
    batch = scheduler.take(0, 0.1)
    assert batch == [2]
    seen[0].extend(batch)
    for worker in range(2):
        while batch := scheduler.take(worker, 0.1, rebalance=False):
            seen[worker].extend(batch)
    assert 3 in seen[0] and 5 in seen[0]
    assert sorted(seen[0] + seen[1]) == list(range(10))
    assert all(items == sorted(items) for items in seen)
    for group in groups:
        assert any(set(group) <= set(items) for items in seen)


def test_rebalance_keeps_started_affinity_and_reserved_lookahead_in_place():
    indices = [90, 80, 70, 60, 50, 40, 30, 20]
    groups = [[90], [80, 40], [70, 50], [60], [30], [20]]
    plans = [[[90], [70, 50], [30]], [[80], [60], [40], [20]]]
    scheduler = WorkScheduler(indices, groups, plans, {**dict.fromkeys(indices, 1), 90: 8, 80: 8})
    assert scheduler.take(0, 0, rebalance=False) == [90]
    assert scheduler.take(1, 0, rebalance=False) == [80]
    # Reserving lookahead advances the boundary even before it executes.
    assert scheduler.take(0, 0, rebalance=False) == [70, 50]
    scheduler.started(0, 90, 0)
    scheduler.started(1, 80, 0)
    scheduler.completed(0, 90)
    received = []
    while batch := scheduler.take(0, 0.1):
        received.extend(batch)
    assert received == [30, 20]
    # 60 is before the promised next item; 40 belongs to the group that started
    # with 80. Neither is eligible even though moving either would reduce load.
    donor = []
    while batch := scheduler.take(1, 0.1, rebalance=False):
        donor.extend(batch)
    assert donor == [60, 40]


@pytest.mark.parametrize("seed", range(20))
def test_adaptive_dispatch_preserves_inventory_affinity_and_order_with_inaccurate_history(seed):
    randomizer = random.Random(seed)
    indices = randomizer.sample(range(1000), 120)
    positions = {index: rank for rank, index in enumerate(indices)}
    groups = [indices[offset::30] for offset in range(30)]
    estimates = {index: randomizer.uniform(0.01, 5) for index in indices}
    actual = {index: randomizer.uniform(0.01, 5) for index in indices}
    plans = plan_workers(indices, groups, 4, estimates)
    scheduler = WorkScheduler(indices, groups, plans, estimates)
    pending = [deque(scheduler.take(worker, 0, rebalance=False)) for worker in range(4)]
    seen = [[] for _ in pending]
    events = []

    def dispatch(worker, now):
        if len(pending[worker]) == 1:
            pending[worker].extend(scheduler.take(worker, now) or [])
        if pending[worker]:
            index = pending[worker].popleft()
            seen[worker].append(index)
            scheduler.started(worker, index, now)
            heapq.heappush(events, (now + actual[index], worker, index))

    for worker in range(4):
        dispatch(worker, 0)
    while events:
        now, worker, index = heapq.heappop(events)
        scheduler.completed(worker, index)
        dispatch(worker, now)
    assert sorted(index for sequence in seen for index in sequence) == sorted(indices)
    assert all(sequence == sorted(sequence, key=positions.__getitem__) for sequence in seen)
    for group in groups:
        assert any(set(group) <= set(sequence) for sequence in seen)

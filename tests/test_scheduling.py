import math

import pytest

from bistra.scheduling import estimate_durations, plan_workers


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

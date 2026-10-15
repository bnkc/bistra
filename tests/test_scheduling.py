import math

import pytest

from bistra.scheduling import GroupQueue, prioritize_groups


def test_prioritize_groups_moves_late_expensive_tests_to_separate_front_jobs():
    groups = [[0], [1], [2], [3]]
    prioritized = prioritize_groups(groups, {0: 0.1, 1: 0.2, 2: 6.7, 3: 15.2})
    assert prioritized == [[3], [2], [1], [0]]
    assert groups == [[0], [1], [2], [3]]


def test_prioritize_groups_preserves_fixture_consumers_and_stable_ties():
    groups = [[0, 1], [2], [3, 4], [5]]
    assert prioritize_groups(groups, {0: 1, 1: 2, 2: 4, 3: 8, 4: 0, 5: 8}) == [
        [3, 4], [5], [2], [0, 1]
    ]


def test_prioritize_groups_uses_selected_positive_mean_for_unknown_and_invalid():
    durations = {
        0: "2", 1: 4, 2: 0, 3: -1, 4: math.nan, 5: math.inf,
        6: "bad", 7: True, 99: 10000,
    }
    assert prioritize_groups([[index] for index in range(9)], durations) == [
        [1], [3], [4], [5], [6], [7], [8], [0], [2]
    ]


def test_prioritize_groups_without_positive_history_uses_one_second():
    assert prioritize_groups([[0], [1, 2], [3]], {0: 0, 99: 10000}) == [
        [1, 2], [3], [0]
    ]
    assert prioritize_groups([], {}) == []


@pytest.mark.parametrize("groups", [[[0, 0]], [[0], [0]]])
def test_prioritize_groups_rejects_duplicate_jobs(groups):
    with pytest.raises(ValueError, match="unique"):
        prioritize_groups(groups, {})


def test_prioritize_groups_rejects_empty_groups():
    with pytest.raises(ValueError, match="empty"):
        prioritize_groups([[0], []], {})


@pytest.mark.parametrize("groups", [[[0, 1]], [[0], [1]]])
def test_prioritize_groups_rejects_unrepresentable_total(groups):
    with pytest.raises(ValueError, match="too large"):
        prioritize_groups(groups, {0: 1e308, 1: 1e308})


def test_reservations_mix_priority_and_shortest_without_splitting_or_repeating():
    groups = [[0, 1], [2], [3, 4], [5]]
    queue = GroupQueue(groups, {0: 5, 1: 5, 2: 3, 3: 1, 4: 1, 5: 2})
    assert queue.take(shortest=True) == [3, 4]
    assert queue.take() == [0, 1]
    assert queue.take(shortest=True) == [5]
    assert queue.take() == [2]
    assert queue.take() is None
    assert not queue
    assert groups == [[0, 1], [2], [3, 4], [5]]


def test_reservations_without_history_preserve_supplied_order():
    queue = GroupQueue([[2], [0, 1]], {})
    assert queue.take() == [2]
    assert queue.take(shortest=True) == [0, 1]
    assert queue.take() is None

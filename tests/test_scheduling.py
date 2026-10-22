import pytest

from bistra.scheduling import Batch, Collection, Scheduler
from bistra.timings import Timing


def finish(scheduler: Scheduler, worker: int, batch: Batch, now: float) -> None:
    for index in batch:
        scheduler.started(worker, index, now=now)
        scheduler.completed(worker, index)


def drain(scheduler: Scheduler, workers: int) -> list[list[int]]:
    count = sum(len(scheduler.unfinished(worker)) for worker in range(workers))
    executed: list[list[int]] = [[] for _ in range(workers)]
    for step in range(count + 1):
        progress = False
        for worker in range(workers):
            batch = scheduler.dispatch(worker, now=float(step))
            if batch is not None:
                progress = True
                finish(scheduler, worker, batch, float(step))
                executed[worker].extend(batch)
        if not progress:
            return executed
    raise AssertionError("scheduler did not finish")


@pytest.mark.parametrize("duration", [None, 0.0, 1e308], ids=["cold", "zero-duration", "overflowing-history"])
def test_required_groups_stay_together_without_losing_order_or_tests(duration: float | None) -> None:
    collection = Collection(
        nodeids=tuple(f"test_{index}" for index in range(12)),
        required_groups=((0, 4), (4, 8), (1, 5, 9)),
        fixture_groups=(),
    )
    history = {} if duration is None else {nodeid: Timing(duration) for nodeid in collection.nodeids}
    scheduler = Scheduler(collection, history, workers=3)

    executed = drain(scheduler, 3)

    assert sorted(index for indices in executed for index in indices) == list(range(12))
    assert all(indices == sorted(indices) for indices in executed)
    assert any({0, 4, 8} <= set(indices) for indices in executed)
    assert any({1, 5, 9} <= set(indices) for indices in executed)
    assert scheduler.completed_count == 12
    assert all(scheduler.current(worker) is None and not scheduler.unfinished(worker) for worker in range(3))


def test_history_balances_expensive_tests() -> None:
    durations = [9, 1, 9, 1, 1, 1]
    collection = Collection(tuple(f"test_{index}" for index in range(6)), (), ())
    history = {nodeid: Timing(duration) for nodeid, duration in zip(collection.nodeids, durations, strict=True)}

    warm = drain(Scheduler(collection, history, workers=2), 2)

    warm_finish = max(sum(durations[index] for index in indices) for indices in warm)
    assert warm_finish == sum(durations) / 2


@pytest.mark.parametrize(
    ("records", "used_workers"),
    [
        ([], 1),
        ([Timing(1)] * 4, 1),
        ([Timing(1, setup=0, teardown=0)] * 4, 2),
    ],
    ids=["cold", "unknown-phases", "parallel-work"],
)
def test_fixture_locality_splits_only_with_evidence_of_a_benefit(records: list[Timing], used_workers: int) -> None:
    collection = Collection(("a", "b", "c", "d"), ((0, 2),), ((0, 1, 2, 3),))
    history = dict(zip(collection.nodeids, records, strict=False))

    executed = drain(Scheduler(collection, history, workers=2), 2)

    assert sum(bool(indices) for indices in executed) == used_workers
    assert sorted(index for indices in executed for index in indices) == [0, 1, 2, 3]
    assert any({0, 2} <= set(indices) for indices in executed)


def test_shared_fixture_cost_is_paid_once_per_worker() -> None:
    collection = Collection(tuple("abcdef"), (), (tuple(range(6)),))
    history = {nodeid: Timing(1, setup=0, teardown=0) for nodeid in collection.nodeids}
    history["a"] = Timing(4, setup=3, teardown=0)

    executed = drain(Scheduler(collection, history, workers=2), 2)

    assert sorted(index for indices in executed for index in indices) == list(range(6))
    # Each worker pays three seconds of setup, then one second per test.
    assert max(3 + len(indices) for indices in executed if indices) == 6


def test_rebalancing_moves_unstarted_work_but_preserves_lookahead_ownership() -> None:
    collection = Collection(tuple(f"test_{index}" for index in range(12)), ((3, 8),), ())
    durations = [1, 1, 8, 1, 1, 1, 1, 1, 2, 2, 2, 4]
    history = {nodeid: Timing(duration) for nodeid, duration in zip(collection.nodeids, durations, strict=True)}
    scheduler = Scheduler(collection, history, workers=2)
    donor = next(worker for worker in range(2) if 3 in scheduler.unfinished(worker))
    recipient = 1 - donor
    original = set(scheduler.unfinished(donor))
    first = scheduler.dispatch(donor, now=0)
    assert first is not None
    scheduler.started(donor, first[0], now=0)
    dispatched = first
    while 3 not in dispatched:
        batch = scheduler.dispatch(donor, now=0)
        assert batch is not None
        dispatched += batch

    received: list[int] = []
    for now in range(1, 13):
        batch = scheduler.dispatch(recipient, now=float(now))
        if batch is None:
            break
        finish(scheduler, recipient, batch, float(now))
        received.extend(batch)

    assert original.intersection(received)
    assert not set(received).intersection((*dispatched, 8))
    assert scheduler.current(donor) == first[0]
    assert received == sorted(received)

    scheduler.completed(donor, first[0])
    finish(scheduler, donor, dispatched[1:], 20)
    retained = list(dispatched)
    for now in range(21, 34):
        batch = scheduler.dispatch(donor, now=float(now))
        if batch is None:
            break
        finish(scheduler, donor, batch, float(now))
        retained.extend(batch)

    assert retained == sorted(retained)
    assert sorted((*received, *retained)) == list(range(12))
    assert scheduler.completed_count == 12


def test_out_of_order_worker_events_are_rejected_without_advancing_progress() -> None:
    scheduler = Scheduler(Collection(("a", "b"), ((0, 1),), ()), {}, workers=1)
    assert scheduler.dispatch(0, now=0) == (0, 1)

    with pytest.raises(ValueError, match="started unexpected test"):
        scheduler.started(0, 1, now=0)
    scheduler.started(0, 0, now=0)
    with pytest.raises(ValueError, match="completed unexpected test"):
        scheduler.completed(0, 1)

    assert scheduler.current(0) == 0
    assert scheduler.completed_count == 0
    scheduler.completed(0, 0)
    finish(scheduler, 0, (1,), 1)
    assert scheduler.completed_count == 2

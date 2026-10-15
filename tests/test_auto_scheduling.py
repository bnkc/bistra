from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from collections import Counter

import pytest
from bistra.durations import load_history, save_history

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="requires POSIX workers"
)


def _history_path(tmp_path):
    key = hashlib.sha256((str(tmp_path) + sys.version).encode()).hexdigest()[:24]
    return tmp_path / "cache" / "bistra" / f"{key}.json"


def _run(tmp_path, *, run="run", workers=2):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    records = tmp_path / run
    records.mkdir()
    env = os.environ.copy()
    env.pop("PYTEST_PLUGINS", None)
    env["PYTEST_ADDOPTS"] = ""
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env["TEST_RECORDS"] = str(records)
    result = subprocess.run(
        [sys.executable, "-m", "bistra", f"-j{workers}", "--", "-q"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return records


def _assert_complete_history(tmp_path, nodeids):
    history = load_history(_history_path(tmp_path))
    assert set(history) == set(nodeids)
    for phases in history.values():
        assert set(phases) == {"total", "setup", "call", "teardown"}
        assert all(value >= 0 for value in phases.values())
        assert phases["total"] >= sum(
            phases[phase] for phase in ("setup", "call", "teardown")
        )
    return history


def test_learns_parametrized_durations_then_starts_expensive_tests_on_separate_workers(
    tmp_path,
):
    (tmp_path / "test_cases.py").write_text(
        textwrap.dedent("""
        import hashlib
        import json
        import os
        from pathlib import Path
        import time
        import pytest

        @pytest.fixture(autouse=True)
        def measured_setup_and_teardown():
            time.sleep(0.003)
            yield
            time.sleep(0.003)

        def record(request):
            nodeid = request.node.nodeid
            destination = Path(os.environ["TEST_RECORDS"]) / hashlib.sha256(nodeid.encode()).hexdigest()
            destination.write_text(json.dumps({
                "nodeid": nodeid, "worker": os.getpid(), "start": time.monotonic(),
            }))

        @pytest.mark.parametrize("index", range(8))
        def test_cheap(index, request):
            record(request)
            time.sleep(0.005)

        @pytest.mark.parametrize("delay", [0.15, 0.25], ids=["slow", "slower"])
        def test_expensive(delay, request):
            record(request)
            time.sleep(delay)
    """)
    )
    nodeids = [f"test_cases.py::test_cheap[{index}]" for index in range(8)] + [
        "test_cases.py::test_expensive[slow]",
        "test_cases.py::test_expensive[slower]",
    ]
    assert not _history_path(tmp_path).exists()
    cold = _run(tmp_path, run="cold")
    history = _assert_complete_history(tmp_path, nodeids)
    assert all(value > 0 for phases in history.values() for value in phases.values())
    assert history[nodeids[-1]]["call"] > history[nodeids[0]]["call"]
    cold_records = sorted(
        (json.loads(path.read_text()) for path in cold.iterdir()),
        key=lambda row: row["start"],
    )
    assert "test_cheap" in cold_records[0]["nodeid"]

    warm = _run(tmp_path, run="warm")
    warm_records = sorted(
        (json.loads(path.read_text()) for path in warm.iterdir()),
        key=lambda row: row["start"],
    )
    first_per_worker = {}
    for record in warm_records:
        first_per_worker.setdefault(record["worker"], record["nodeid"])
    assert set(first_per_worker.values()) == set(nodeids[-2:])
    assert len(first_per_worker) == 2
    assert Counter(row["nodeid"] for row in warm_records) == Counter(nodeids)
    _assert_complete_history(tmp_path, nodeids)


def test_history_includes_original_and_fresh_worker_protocol_phases(tmp_path):
    (tmp_path / "test_cases.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import time
        import pytest

        COLLECTOR_PID = os.getpid()

        @pytest.fixture(autouse=True)
        def lifecycle():
            time.sleep(0.003)
            yield
            time.sleep(0.003)

        def record(name, index, request):
            Path(os.environ["TEST_RECORDS"], f"{name}-{index}.json").write_text(json.dumps({
                "nodeid": request.node.nodeid, "pid": os.getpid(), "collector": COLLECTOR_PID,
            }))

        @pytest.mark.parametrize("index", [0, 1], ids=["native-a", "native-b"])
        def test_parent(index, request):
            record("parent", index, request)
            time.sleep(0.03)

        @pytest.mark.parametrize("index", [0, 1], ids=["ordinary-a", "ordinary-b"])
        def test_worker(index, request):
            record("worker", index, request)
            time.sleep(0.01)
    """)
    )
    records = _run(tmp_path)
    rows = [json.loads(path.read_text()) for path in records.iterdir()]
    assert len(rows) == 4
    assert all(
        row["pid"] == row["collector"] for row in rows if "test_parent" in row["nodeid"]
    )
    assert all(row["pid"] == row["collector"] for row in rows)
    assert len({row["pid"] for row in rows}) == 2
    history = _assert_complete_history(tmp_path, [row["nodeid"] for row in rows])
    assert all(value > 0 for phases in history.values() for value in phases.values())


def test_long_test_does_not_reserve_expensive_followup_behind_it(tmp_path):
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import time
        import pytest

        RECORDS = Path(os.environ["TEST_RECORDS"])

        def wait_for(name):
            deadline = time.monotonic() + 5
            while not (RECORDS / name).exists():
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.001)
            return True

        @pytest.fixture(autouse=True)
        def record_execution(request):
            with (RECORDS / f"executions-{os.getpid()}.jsonl").open("a") as output:
                output.write(json.dumps(request.node.nodeid) + "\\n")
    """)
    )
    (tmp_path / "test_long.py").write_text(
        textwrap.dedent("""
        import json
        from conftest import RECORDS, wait_for

        def test_long():
            (RECORDS / "long-started").touch()
            overlapped = wait_for("medium-completed")
            (RECORDS / "overlapped.json").write_text(json.dumps(overlapped))
    """)
    )
    (tmp_path / "test_initial.py").write_text(
        textwrap.dedent("""
        import pytest
        from conftest import wait_for

        @pytest.fixture(scope="module", autouse=True)
        def shared_resource():
            yield

        def test_first():
            assert wait_for("long-started")

        def test_second():
            pass
    """)
    )
    (tmp_path / "test_remaining.py").write_text(
        textwrap.dedent("""
        from conftest import RECORDS

        def test_medium():
            (RECORDS / "medium-completed").touch()

        def test_tiny():
            pass
    """)
    )
    history = {
        "test_long.py::test_long": {"total": 100},
        "test_initial.py::test_first": {"total": 10},
        "test_initial.py::test_second": {"total": 10},
        "test_remaining.py::test_medium": {"total": 10},
        "test_remaining.py::test_tiny": {"total": 0.001},
    }
    save_history(_history_path(tmp_path), history)
    records = _run(tmp_path)
    executions = [
        json.loads(line)
        for path in records.glob("executions-*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert Counter(executions) == Counter(history.keys())
    assert json.loads((records / "overlapped.json").read_text()), (
        "The medium test was reserved behind the long test instead of running "
        "on the other worker while the long test was active."
    )
    updated = load_history(_history_path(tmp_path))
    assert set(updated) == set(history)
    for phases in updated.values():
        assert set(phases) == {"total", "setup", "call", "teardown"}
        assert all(value >= 0 for value in phases.values())


def test_warm_scheduling_preserves_scoped_fixture_lifetimes_and_consumer_order(
    tmp_path,
):
    package = tmp_path / "cases"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import pytest

        def record(kind, **data):
            with Path(os.environ["TEST_RECORDS"], "lifecycle.jsonl").open("a") as output:
                output.write(json.dumps({"kind": kind, "pid": os.getpid(), **data}) + "\\n")

        def lifetime(scope, owner, value):
            record("fixture", scope=scope, owner=owner, value=value, phase="setup")
            yield value
            record("fixture", scope=scope, owner=owner, value=value, phase="teardown")

        @pytest.fixture(scope="session", params=["red", "blue"])
        def edition(request):
            yield from lifetime("session", "session", request.param)

        @pytest.fixture(scope="package")
        def package_resource(edition):
            yield from lifetime("package", "cases", edition)

        @pytest.fixture(scope="module")
        def module_resource(request, package_resource):
            yield from lifetime("module", request.module.__name__, package_resource)

        @pytest.fixture(scope="class")
        def class_resource(request, module_resource):
            yield from lifetime("class", request.cls.__name__, module_resource)
    """)
    )
    (package / "test_a.py").write_text(
        textwrap.dedent("""
        from .conftest import record

        class TestScoped:
            def test_first(self, class_resource, request):
                record("test", nodeid=request.node.nodeid, value=class_resource)

            def test_second(self, class_resource, request):
                record("test", nodeid=request.node.nodeid, value=class_resource)

        def test_third(module_resource, request):
            record("test", nodeid=request.node.nodeid, value=module_resource)
    """)
    )
    (package / "test_b.py").write_text(
        textwrap.dedent("""
        from .conftest import record

        def test_fourth(module_resource, request):
            record("test", nodeid=request.node.nodeid, value=module_resource)
    """)
    )
    (tmp_path / "test_unrelated.py").write_text("def test_independent():\n    pass\n")
    cases = [
        "cases/test_a.py::TestScoped::test_first",
        "cases/test_a.py::TestScoped::test_second",
        "cases/test_a.py::test_third",
        "cases/test_b.py::test_fourth",
    ]
    history = {
        f"{nodeid}[{value}]": {"total": cost}
        for value in ("red", "blue")
        for nodeid, cost in zip(cases, (1, 10, 2, 20))
    }
    history["test_unrelated.py::test_independent"] = {"total": 15}
    save_history(_history_path(tmp_path), history)
    records = _run(tmp_path)
    rows = [
        json.loads(line)
        for line in (records / "lifecycle.jsonl").read_text().splitlines()
    ]
    fixtures = [row for row in rows if row["kind"] == "fixture"]
    expected = Counter(
        (scope, owner, value, phase)
        for scope, owner in (
            ("session", "session"),
            ("package", "cases"),
            ("module", "cases.test_a"),
            ("module", "cases.test_b"),
            ("class", "TestScoped"),
        )
        for value in ("red", "blue")
        for phase in ("setup", "teardown")
    )
    assert (
        Counter(
            (row["scope"], row["owner"], row["value"], row["phase"]) for row in fixtures
        )
        == expected
    )
    for value in ("red", "blue"):
        calls = [row for row in rows if row["kind"] == "test" and row["value"] == value]
        assert [row["nodeid"] for row in calls] == [
            f"{nodeid}[{value}]" for nodeid in cases
        ]
        assert len({row["pid"] for row in calls}) == 1
    _assert_complete_history(tmp_path, history)


@pytest.mark.parametrize("fixture_kind", ["indirect-module", "package-fallback"])
def test_warm_groups_follow_effective_fixture_scope(tmp_path, fixture_kind):
    declaration = (
        "@pytest.fixture"
        if fixture_kind == "indirect-module"
        else '@pytest.fixture(scope="package", params=["red", "blue"])'
    )
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import pytest

        def record(phase, value, nodeid=None):
            with Path(os.environ["TEST_RECORDS"], "lifecycle.jsonl").open("a") as output:
                output.write(json.dumps({
                    "phase": phase, "value": value, "nodeid": nodeid, "pid": os.getpid(),
                }) + "\\n")
    """)
        + f"\n{declaration}\n"
        + textwrap.dedent("""
        def resource(request):
            record("setup", request.param)
            yield request.param
            record("teardown", request.param)
    """)
    )
    decoration = (
        '@pytest.mark.parametrize("resource", ["red", "blue"], indirect=True, scope="module")\n'
        if fixture_kind == "indirect-module"
        else ""
    )
    (tmp_path / "test_cases.py").write_text(
        "import time\nimport pytest\nfrom conftest import record\n\n"
        + "\n".join(
            decoration + f"def test_{name}(resource, request):\n"
            '    record("call", resource, request.node.nodeid)\n'
            "    time.sleep(0.01)\n"
            for name in ("first", "second", "third")
        )
    )
    history = {
        f"test_cases.py::test_{name}[{value}]": {"total": cost}
        for value in ("red", "blue")
        for name, cost in (("first", 100), ("second", 1), ("third", 20))
    }
    save_history(_history_path(tmp_path), history)
    records = _run(tmp_path)
    rows = [
        json.loads(line)
        for line in (records / "lifecycle.jsonl").read_text().splitlines()
    ]
    for value in ("red", "blue"):
        events = [row for row in rows if row["value"] == value]
        assert [row["phase"] for row in events] == [
            "setup",
            "call",
            "call",
            "call",
            "teardown",
        ]
        assert [row["nodeid"] for row in events if row["phase"] == "call"] == [
            f"test_cases.py::test_{name}[{value}]"
            for name in ("first", "second", "third")
        ]
        assert len({row["pid"] for row in events}) == 1

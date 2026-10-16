"""Exercise shared resources through ordinary pytest and xdist lifecycle hooks."""

import json
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize(
    "failure", ["none", "provision", "configure", "collect", "test", "crash", "finish", "release", "idle"]
)
def test_shared_resources_survive_workers_and_are_always_released(tmp_path, subprocess_env, failure):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import pytest

        FAILURE = os.environ["INJECT_FAILURE"]
        WORKER = os.environ.get("PYTEST_XDIST_WORKER")
        if WORKER:
            assert Path(os.environ["SHARED_RESOURCE"]).is_dir()
            assert Path("resources", WORKER).is_dir()
            Path(f"worker-{os.getpid()}").touch()

        def record(kind, **details):
            with open("events.jsonl", "a") as out:
                out.write(json.dumps({"kind": kind, **details}) + "\\n")

        def pytest_configure(config):
            if WORKER:
                assert config.workerinput["resource"] == str(Path("resources", WORKER).resolve())
                if FAILURE == "configure":
                    raise pytest.UsageError("injected worker configure failure")
            else:
                assert not hasattr(config, "workerinput")
                record("controller_configure")

        @pytest.hookimpl(wrapper=True)
        def pytest_sessionstart(session):
            yield
            if not WORKER:
                Path("shared").mkdir()
                os.environ["SHARED_RESOURCE"] = str(Path("shared").resolve())
                record("controller_start")

        def pytest_configure_node(node):
            assert Path(os.environ["SHARED_RESOURCE"]).is_dir()
            name = node.gateway.id
            path = Path("resources", name)
            path.mkdir(parents=True)
            node.workerinput["resource"] = str(path.resolve())
            record("provision", worker=name)
            if FAILURE == "provision":
                raise RuntimeError("injected provisioning failure")

        def pytest_collection_modifyitems(config):
            if WORKER and FAILURE == "collect":
                raise RuntimeError("injected collection failure")

        @pytest.fixture
        def resource(request):
            path = Path(request.config.workerinput["resource"])
            assert path.is_dir()
            yield path
            assert path.is_dir()
            record("fixture_finish", worker=WORKER)

        def pytest_sessionfinish(session):
            if WORKER:
                assert Path(os.environ["SHARED_RESOURCE"]).is_dir()
                assert Path(session.config.workerinput["resource"]).is_dir()
                session.config.workeroutput["finished"] = True
                record("worker_finish", worker=WORKER)
                if FAILURE == "finish":
                    raise RuntimeError("injected worker cleanup failure")
            else:
                assert not list(Path("resources").glob("*"))
                Path("shared").rmdir()
                record("controller_finish")

        def pytest_testnodedown(node, error):
            assert Path(os.environ["SHARED_RESOURCE"]).is_dir()
            name = node.gateway.id
            record("down", worker=name, error=error, output=node.workeroutput)
            Path(node.workerinput["resource"]).rmdir()
            if FAILURE == "release":
                raise RuntimeError("injected resource release failure")
    """)
    )
    (tmp_path / "test_resource.py").write_text(
        textwrap.dedent("""
        import os
        import pytest

        @pytest.mark.parametrize("index", range(1 if os.environ["INJECT_FAILURE"] == "idle" else 12))
        def test_resource(index, resource):
            assert resource.is_dir()
            if os.environ["INJECT_FAILURE"] == "crash":
                os._exit(17)
            if os.environ["INJECT_FAILURE"] == "test":
                assert False, "injected test failure"
    """)
    )
    result = subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "2", "--", "-q"],
        cwd=tmp_path,
        env={**subprocess_env, "INJECT_FAILURE": failure},
        text=True,
        capture_output=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    assert (result.returncode == 0) == (failure in ("none", "idle")), output
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [row["kind"] for row in events[:2]] == ["controller_configure", "controller_start"]
    provisioned = [row["worker"] for row in events if row["kind"] == "provision"]
    closed = [row for row in events if row["kind"] == "down"]
    assert sorted(row["worker"] for row in closed) == sorted(provisioned), output
    assert not list((tmp_path / "resources").iterdir())
    # pytest does not call sessionfinish when sessionstart itself fails.
    if failure != "provision":
        assert events[-1]["kind"] == "controller_finish", output
        assert not (tmp_path / "shared").exists()
    if failure in ("none", "test", "idle"):
        assert all(row["error"] is None and row["output"] == {"finished": True} for row in closed), output
        expected = {"none": "12 passed", "test": "12 failed", "idle": "1 passed"}
        assert expected[failure] in output
    if failure in ("provision", "configure", "collect", "crash", "finish"):
        assert any(row["error"] for row in closed), output
    for path in tmp_path.glob("worker-*"):
        with pytest.raises(ProcessLookupError):
            os.kill(int(path.name.removeprefix("worker-")), 0)

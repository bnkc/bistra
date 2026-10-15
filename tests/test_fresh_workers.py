import json
import os
import subprocess
import sys
import textwrap

import pytest


def run(tmp_path, subprocess_env, *args):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    return subprocess.run(
        [sys.executable, "-m", "bistra", "-j", "3", "--", "-q", *args],
        cwd=tmp_path,
        env=subprocess_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


@pytest.mark.parametrize(
    "problem",
    [
        "nodeids",
        "fixtures",
        "collection-error",
        "import-error",
        "crash",
        "noisy-crash",
        "timeout",
        "parent-error",
    ],
)
def test_collection_problems_run_no_tests_and_reap_workers(tmp_path, subprocess_env, problem):
    conftest = """
        import multiprocessing
        import os
        from pathlib import Path
        import time
        import pytest

        CHILD = multiprocessing.parent_process() is not None
        Path(f"collector-{os.getpid()}").touch()

        def pytest_collection_finish(session):
            pass
    """
    test_source = """
        from pathlib import Path
        import pytest

        @pytest.fixture(autouse=True)
        def must_not_set_up():
            Path("unexpected-setup").touch()

        def test_first(resource):
            Path("unexpected-test").touch()

        def test_second(resource):
            Path("unexpected-test").touch()
    """
    conftest = textwrap.dedent(conftest)
    scope = '("module" if CHILD else "function")' if problem == "fixtures" else '"function"'
    conftest += f"\n@pytest.fixture(scope={scope})\ndef resource():\n    return 1\n"
    if problem == "nodeids":
        conftest += "\ndef pytest_collection_modifyitems(items):\n    items[0]._nodeid += str(os.getpid())\n"
    elif problem == "collection-error":
        conftest += (
            "\ndef pytest_collection_modifyitems(items):\n    if CHILD:\n"
            '        raise RuntimeError("child collection evidence")\n'
        )
    elif problem in {"crash", "noisy-crash"}:
        conftest += "\ndef pytest_collection_modifyitems(items):\n    if CHILD:\n"
        if problem == "noisy-crash":
            conftest += (
                '        os.write(2, b"discarded-startup-prefix" + b"x" * 20000 '
                '+ b"recent startup evidence: \\xff\\n")\n'
            )
        conftest += "        os._exit(17)\n"
    elif problem == "timeout":
        conftest += (
            "\ndef pytest_collection_finish(session):\n    if CHILD:\n        time.sleep(30)\n"
            "    else:\n        import bistra.fresh\n        bistra.fresh.COLLECTION_TIMEOUT = 0.15\n"
        )
    elif problem == "parent-error":
        conftest += (
            "\ndef pytest_collection_finish(session):\n    if not CHILD:\n        deadline = time.monotonic() + 5\n"
            '        while len(list(Path().glob("collector-*"))) < 3 and time.monotonic() < deadline:\n'
            '            time.sleep(0.01)\n        raise pytest.UsageError("parent collection evidence")\n'
        )
    (tmp_path / "conftest.py").write_text(conftest)
    test_source = textwrap.dedent(test_source)
    if problem == "import-error":
        test_source = (
            "import multiprocessing\nif multiprocessing.parent_process() is not None:\n"
            "    raise ImportError('child import evidence')\n" + test_source
        )
    (tmp_path / "test_cases.py").write_text(test_source)
    result = run(tmp_path, subprocess_env, "--continue-on-collection-errors")
    assert result.returncode == 4, result.stdout + result.stderr
    expected = {
        "nodeids": "collection differs",
        "fixtures": "collection differs",
        "collection-error": "child collection evidence",
        "import-error": "child import evidence",
        "crash": "17",
        "noisy-crash": "recent startup evidence: \ufffd",
        "timeout": "collection timed out",
        "parent-error": "parent collection evidence",
    }[problem]
    assert expected in result.stdout + result.stderr
    if problem == "noisy-crash":
        output = result.stdout + result.stderr
        assert "discarded-startup-prefix" not in output
        assert output.count("x") < 12000
    assert not (tmp_path / "unexpected-setup").exists()
    assert not (tmp_path / "unexpected-test").exists()
    collectors = list(tmp_path.glob("collector-*"))
    assert len(collectors) >= 2
    for path in collectors:
        with pytest.raises(ProcessLookupError):
            os.kill(int(path.name.removeprefix("collector-")), 0)


@pytest.mark.parametrize("option,count", [("--collect-only", 1), ("--help", 0), ("--version", 0)])
def test_nonexecuting_commands_do_not_start_workers(tmp_path, subprocess_env, option, count):
    (tmp_path / "conftest.py").write_text('import os\nassert "PYTEST_XDIST_WORKER" not in os.environ\n')
    (tmp_path / "test_cases.py").write_text(
        textwrap.dedent("""
        import os
        from pathlib import Path
        Path(f"collector-{os.getpid()}").touch()

        def test_unused():
            raise AssertionError("must not execute")
    """)
    )
    result = run(tmp_path, subprocess_env, option)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list(tmp_path.glob("collector-*"))) == count


@pytest.mark.parametrize("load_xdist", [False, True])
def test_worker_identity_precedes_project_configuration(tmp_path, subprocess_env, load_xdist):
    (tmp_path / "early_plugin.py").write_text(
        textwrap.dedent("""
        import os
        import pytest

        @pytest.hookimpl(tryfirst=True)
        def pytest_load_initial_conftests(early_config):
            worker = os.environ["PYTEST_XDIST_WORKER"]
            assert early_config.workerinput["workerid"] == worker
            assert os.environ["RESOURCE_PREFIX"] == "original"
            os.environ["RESOURCE_PREFIX"] += "/" + worker
    """)
    )
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import pytest

        IMPORT_IDENTITY = {
            "workerid": os.environ["PYTEST_XDIST_WORKER"],
            "workercount": int(os.environ["PYTEST_XDIST_WORKER_COUNT"]),
            "testrunuid": os.environ["PYTEST_XDIST_TESTRUNUID"],
        }
        RESOURCE = os.environ["RESOURCE_PREFIX"]
        assert RESOURCE == "original/" + IMPORT_IDENTITY["workerid"]
        Path(f"import-{os.getpid()}.json").write_text(json.dumps(IMPORT_IDENTITY))

        @pytest.hookimpl(tryfirst=True)
        def pytest_configure(config):
            assert config.workerinput == IMPORT_IDENTITY

        @pytest.fixture
        def resource(request):
            assert request.config.workerinput == IMPORT_IDENTITY
            yield RESOURCE
            assert os.environ["PYTEST_XDIST_WORKER"] == IMPORT_IDENTITY["workerid"]
    """)
    )
    (tmp_path / "test_cases.py").write_text(
        textwrap.dedent("""
        import os
        import time
        from pathlib import Path
        import pytest

        COLLECTED_WORKER = os.environ["PYTEST_XDIST_WORKER"]

        @pytest.mark.parametrize("index", range(12))
        def test_resource(index, request, resource, worker_id, testrun_uid):
            assert resource == "original/" + worker_id
            assert worker_id == COLLECTED_WORKER
            assert testrun_uid == request.config.workerinput["testrunuid"]
            Path(f"started-{worker_id}").touch()
            deadline = time.monotonic() + 5
            while len(list(Path().glob("started-*"))) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(list(Path().glob("started-*"))) == 3
    """)
    )
    args = ["-p", "early_plugin"]
    if load_xdist:
        args += ["-p", "xdist.plugin"]
    env = {**subprocess_env, "RESOURCE_PREFIX": "original"}
    result = run(tmp_path, env, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "12 passed" in result.stdout
    identities = [json.loads(path.read_text()) for path in tmp_path.glob("import-*.json")]
    assert {item["workerid"] for item in identities} == {
        "bistra0",
        "bistra1",
        "bistra2",
    }
    assert {item["workercount"] for item in identities} == {3}
    assert len({item["testrunuid"] for item in identities}) == 1


def test_embedded_runs_restore_identity_after_success_and_collection_error(tmp_path, subprocess_env):
    (tmp_path / "test_cases.py").write_text("def test_ok():\n    pass\n")
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import os
        import pytest

        def pytest_configure(config):
            assert config.workerinput["workerid"] == os.environ["PYTEST_XDIST_WORKER"]
            if os.environ.get("FAIL_CONFIG"):
                raise pytest.UsageError("requested configuration failure")
    """)
    )
    (tmp_path / "embedded.py").write_text(
        textwrap.dedent("""
        import os
        import pytest

        if __name__ == "__main__":
            keys = ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID")
            os.environ["PYTEST_XDIST_WORKER"] = "calling-process"
            before = {key: os.environ.get(key) for key in keys}
            for fail in (False, True):
                if fail:
                    os.environ["FAIL_CONFIG"] = "1"
                code = pytest.main(["-p", "bistra.plugin", "--bistra", "--bistra-workers=2", "-q"])
                assert code == (4 if fail else 0), code
                assert {key: os.environ.get(key) for key in keys} == before
    """)
    )
    result = subprocess.run(
        [sys.executable, "embedded.py"],
        cwd=tmp_path,
        env=subprocess_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr

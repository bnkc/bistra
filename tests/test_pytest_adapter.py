from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter

SOURCE = """
import os, subprocess, sys
import pytest

@pytest.fixture(scope="session")
def lifetime():
    with open("lifecycle.jsonl", "a") as out:
        out.write(str(os.getpid()) + " setup\\n")
    yield
    with open("lifecycle.jsonl", "a") as out:
        out.write(str(os.getpid()) + " teardown\\n")

@pytest.mark.parametrize("i", range(18))
def test_capture_and_lifetime(i, lifetime, capfd, tmp_path):
    print("python", i)
    os.write(1, b"descriptor\\n")
    subprocess.run([sys.executable, "-c", "print('child')"], check=True)
    out = capfd.readouterr().out
    assert "python" in out and "descriptor" in out and "child" in out
    p = tmp_path / "unique.txt"
    p.write_text(str(i))
    assert p.read_text() == str(i)

def test_failure():
    print("failure evidence")
    assert {"expected": 1} == {"expected": 2}, "intentional assertion"

@pytest.mark.skip(reason="skip evidence")
def test_skip(): pass

@pytest.mark.xfail(reason="xfail evidence")
def test_xfail(): assert False

@pytest.mark.xfail(reason="xpass evidence")
def test_xpass(): pass

@pytest.fixture
def teardown_error():
    yield
    raise RuntimeError("teardown evidence")

def test_teardown(teardown_error): pass
"""


def test_parallel_reports_capture_and_fixture_lifetimes(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_cases.py").write_text(SOURCE)
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    results = []
    for workers in (1, 3):
        xml = tmp_path / f"result-{workers}.xml"
        p = subprocess.run(
            [
                sys.executable,
                "-m",
                "bistra",
                "-j",
                str(workers),
                "--",
                "-q",
                f"--junitxml={xml}",
            ],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
        )
        assert p.returncode == 1, p.stdout + p.stderr
        assert "intentional assertion" in p.stdout
        assert "failure evidence" in p.stdout
        assert "Differing items:" in p.stdout
        results.append(
            Counter(
                (
                    t.get("classname"),
                    t.get("name"),
                    tuple(c.tag for c in t if c.tag in ("failure", "error", "skipped")),
                )
                for t in ET.parse(xml).iter("testcase")
            )
        )
    assert results[0] == results[1]
    records = Counter((tmp_path / "lifecycle.jsonl").read_text().splitlines())
    pids = {line.split()[0] for line in records}
    assert len(pids) == 4
    for pid in pids:
        assert records[f"{pid} setup"] == records[f"{pid} teardown"] == 1

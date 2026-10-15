import json
import pickle
import subprocess
import sys
import textwrap
import warnings

from bistra.pytest_adapter import _deserialize_warning, _serialize_warning


class DetailedWarning(UserWarning):
    def __init__(self, number, description):
        self.number = number
        super().__init__(number, description)

    def __str__(self):
        return f"detail {self.number}: {self.args[1]}"


def test_importable_warning_retains_observer_semantics():
    original = warnings.WarningMessage(
        DetailedWarning(23, "evidence"),
        DetailedWarning,
        "example.py",
        17,
        line="warnings.warn(...)\n",
    )
    data = pickle.loads(pickle.dumps(_serialize_warning(original)))
    restored = _deserialize_warning(data)
    assert restored.category is DetailedWarning
    assert type(restored.message) is DetailedWarning
    assert restored.message.args == original.message.args
    assert restored.message.number == 23
    assert str(restored.message) == str(original.message)
    assert restored.filename == original.filename
    assert restored.lineno == original.lineno
    assert restored.line == original.line
    assert restored._bistra_warning_fallback is False


def test_local_warning_preserves_text_family_and_qualified_metadata():
    class LocalWarning(ResourceWarning):
        pass

    original = warnings.WarningMessage(LocalWarning("local evidence"), LocalWarning, "local.py", 4)
    module = sys.modules[LocalWarning.__module__]
    before = dict(vars(module))
    data = pickle.loads(pickle.dumps(_serialize_warning(original)))
    restored = _deserialize_warning(data)
    assert str(restored.message) == "local evidence"
    assert restored.category.__name__ == "LocalWarning"
    assert issubclass(restored.category, ResourceWarning)
    assert isinstance(restored.message, restored.category)
    assert restored._bistra_warning_metadata["category_class"]["qualname"] == LocalWarning.__qualname__
    assert restored._bistra_warning_fallback is True
    assert dict(vars(module)) == before  # No synthetic types installed in modules.


def test_untransportable_args_and_source_keep_text_without_crashing():
    original = warnings.WarningMessage(UserWarning(lambda: None), UserWarning, "example.py", 2, source=lambda: None)
    data = pickle.loads(pickle.dumps(_serialize_warning(original)))
    restored = _deserialize_warning(data)
    assert str(restored.message) == str(original.message)
    assert restored.category is UserWarning
    assert isinstance(restored.message, Warning)
    assert restored._bistra_warning_fallback is True
    assert "lambda" in restored._bistra_warning_metadata["message_args_repr"]
    assert "lambda" in restored.source


def test_parallel_warning_observers_match_serial_for_reconstructable_warnings(tmp_path, subprocess_env):
    (tmp_path / "pytest.ini").write_text("[pytest]\nfilterwarnings = always\n")
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        def pytest_warning_recorded(warning_message, when, nodeid, location):
            import multiprocessing
            if multiprocessing.parent_process() is not None:
                return
            record = {
                "nodeid": nodeid,
                "category": warning_message.category.__name__,
                "message_class": type(warning_message.message).__name__,
                "text": str(warning_message.message),
                "args": warning_message.message.args,
                "is_warning": isinstance(warning_message.message, Warning),
                "number": getattr(warning_message.message, "number", None),
                "resource": issubclass(warning_message.category, ResourceWarning),
                "filename": warning_message.filename,
                "lineno": warning_message.lineno,
            }
            with open(os.environ["WARNINGS_RECORD"], "a") as output:
                output.write(json.dumps(record) + "\\n")
    """)
    )
    (tmp_path / "test_warnings.py").write_text(
        textwrap.dedent("""
        import warnings
        class DetailedWarning(UserWarning):
            def __init__(self, number, description):
                self.number = number
                super().__init__(number, description)
            def __str__(self):
                return f"detail {self.number}: {self.args[1]}"
        def test_detailed():
            warnings.warn(DetailedWarning(23, "evidence"))
        def test_builtin():
            warnings.warn("builtin evidence", DeprecationWarning)
        def test_local():
            class LocalWarning(ResourceWarning):
                pass
            warnings.warn(LocalWarning("local evidence"))
    """)
    )
    records = []
    for workers in (1, 2):
        path = tmp_path / f"warnings-{workers}.jsonl"
        env = {**subprocess_env, "WARNINGS_RECORD": str(path)}
        command = [sys.executable, "-m", "bistra", "-j", str(workers), "--", "-q"]
        process = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stdout + process.stderr
        assert "LocalWarning: local evidence" in process.stdout
        assert "DetailedWarning: detail 23: evidence" in process.stdout
        records.append(
            sorted(
                (json.loads(line) for line in path.read_text().splitlines()),
                key=lambda item: item["nodeid"],
            )
        )
    assert len(records[0]) == 3
    assert records[0] == records[1]

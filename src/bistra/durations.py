"""Cache test timings for scheduling; tests still run every time."""

import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path

_FIELDS = ("total", "setup", "call", "teardown")


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _record(value: object) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    result = {field: duration for field in _FIELDS if (duration := _duration(value.get(field))) is not None}
    return result if "total" in result else None


def environment_key() -> str:
    """Separate interpreters and virtual environments within a project's history."""
    identity = f"{sys.implementation.name}\0{sys.version}\0{sys.prefix}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def history_path(root: Path) -> Path:
    return root / ".bistra_cache" / "timings.json"


def _environments(path: Path) -> dict[str, dict[str, dict[str, float]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 2:
        return {}
    environments = data.get("environments")
    if not isinstance(environments, dict):
        return {}
    return {
        key: {nodeid: record for nodeid, value in records.items() if (record := _record(value)) is not None}
        for key, records in environments.items()
        if isinstance(records, dict)
    }


def load_history(path: Path) -> dict[str, dict[str, float]]:
    """Read valid timings for the current Python environment."""
    return _environments(path).get(environment_key(), {})


def update_history(
    history: dict[str, dict[str, float]],
    measured: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Average each new measurement with its previous estimate.

    Completed tests supply all four fields, including phases that took zero
    seconds. Missing fields keep their previous estimates.
    """
    updated = {nodeid: dict(record) for nodeid, record in history.items()}
    for nodeid, value in measured.items():
        sample = _record(value)
        if sample is None:
            continue
        record = updated.setdefault(nodeid, {})
        for field, duration in sample.items():
            record[field] = record[field] * 0.5 + duration * 0.5 if field in record else duration
    return updated


def save_history(path: Path, history: dict[str, dict[str, float]]) -> None:
    """Write the cache atomically. Let filesystem errors reach the caller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (path.parent / ".gitignore").open("x", encoding="utf-8") as ignore:
            ignore.write("*\n")
    except FileExistsError:
        pass
    environments = _environments(path)
    environments[environment_key()] = history
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump({"version": 2, "environments": environments}, output, allow_nan=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

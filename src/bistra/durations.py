"""Optional timing history; cached estimates never replace test execution."""

from __future__ import annotations

import json
import math
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
    result = {
        field: duration
        for field in _FIELDS
        if (duration := _duration(value.get(field))) is not None
    }
    return result if "total" in result else None


def load_history(path: Path) -> dict[str, dict[str, float]]:
    """Read valid observations, tolerating absent, corrupt, or legacy caches."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "version" in data:
        if type(data["version"]) is not int or data["version"] != 1:
            return {}
        records = data.get("tests")
        if not isinstance(records, dict):
            return {}
        return {
            nodeid: record
            for nodeid, value in records.items()
            if (record := _record(value)) is not None
        }
    return {
        nodeid: {"total": duration}
        for nodeid, value in data.items()
        if (duration := _duration(value)) is not None
    }


def update_history(
    history: dict[str, dict[str, float]],
    measured: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Merge measurements using equal weight for previous and new estimates.

    Complete protocols supply all four fields, including zero-length phases.
    Missing fields retain their previous estimates, which also allows legacy
    total-only records to acquire phases without inventing past observations.
    """
    updated = {nodeid: dict(record) for nodeid, record in history.items()}
    for nodeid, sample in measured.items():
        sample = _record(sample)
        if sample is None:
            continue
        previous = updated.get(nodeid, {})
        updated[nodeid] = {
            **previous,
            **{
                field: previous[field] * 0.5 + value * 0.5
                if field in previous
                else value
                for field, value in sample.items()
            },
        }
    return updated


def save_history(path: Path, history: dict[str, dict[str, float]]) -> None:
    """Atomically replace the optional cache; filesystem errors reach the caller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f"{path.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump({"version": 1, "tests": history}, output, allow_nan=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

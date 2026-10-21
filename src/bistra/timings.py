import hashlib
import json
import math
import os
import sys
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Timing:
    """Measured seconds; absent phases are unknown rather than zero."""

    total: float
    setup: float | None = None
    call: float | None = None
    teardown: float | None = None


def environment_key() -> str:
    identity = f"{sys.implementation.name}\0{sys.version}\0{sys.prefix}"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def load(root: Path) -> dict[str, Timing]:
    return _read(root / ".bistra_cache" / "timings.json").get(environment_key(), {})


def _merge(history: Mapping[str, Timing], measured: Mapping[str, Timing]) -> dict[str, Timing]:
    """Average new measurements with history, retaining unmeasured tests and phases."""
    updated = dict(history)
    for nodeid, sample in measured.items():
        previous = history.get(nodeid)
        updated[nodeid] = (
            sample
            if previous is None
            else Timing(
                total=previous.total * 0.5 + sample.total * 0.5,
                setup=_average(previous.setup, sample.setup),
                call=_average(previous.call, sample.call),
                teardown=_average(previous.teardown, sample.teardown),
            )
        )
    return updated


def save(root: Path, measured: Mapping[str, Timing]) -> None:
    """Merge measurements with the latest history under an exclusive lock."""
    path = root / ".bistra_cache" / "timings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (path.parent / ".gitignore").open("x", encoding="utf-8") as ignore:
            ignore.write("*\n")
    except FileExistsError:
        pass
    with _locked(path.with_suffix(".lock")):
        environments = _read(path)
        key = environment_key()
        environments[key] = _merge(environments.get(key, {}), measured)
        data = {
            "version": 2,
            "environments": {
                key: {nodeid: _encode(record) for nodeid, record in records.items()}
                for key, records in environments.items()
            },
        }
        _write(path, data)


@contextmanager
def _locked(path: Path) -> Generator[None]:
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write(path: Path, data: Mapping[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix="timings.json.", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(data, output, allow_nan=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _average(previous: float | None, sample: float | None) -> float | None:
    if sample is None:
        return previous
    return sample if previous is None else previous * 0.5 + sample * 0.5


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _decode(value: object) -> Timing | None:
    if not isinstance(value, dict):
        return None
    total = _seconds(value.get("total"))
    if total is None:
        return None
    return Timing(
        total=total,
        setup=_seconds(value.get("setup")),
        call=_seconds(value.get("call")),
        teardown=_seconds(value.get("teardown")),
    )


def _encode(record: Timing) -> dict[str, float]:
    phases = (("setup", record.setup), ("call", record.call), ("teardown", record.teardown))
    return {"total": record.total, **{name: value for name, value in phases if value is not None}}


def _read(path: Path) -> dict[str, dict[str, Timing]]:
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
        key: {nodeid: record for nodeid, value in records.items() if (record := _decode(value)) is not None}
        for key, records in environments.items()
        if isinstance(records, dict)
    }

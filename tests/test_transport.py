import json
import multiprocessing
import os
import signal
import struct
import subprocess
import sys
import textwrap
from multiprocessing.reduction import ForkingPickler

import bistra.executor as executor
import pytest
from bistra.executor import _message_parts, execute_parallel
from bistra.transport import FramedReceiver, TransportError


@pytest.fixture
def channel():
    parent, child = multiprocessing.Pipe()
    receiver = FramedReceiver(parent)
    try:
        yield parent, child, receiver
    finally:
        receiver.close()
        parent.close()
        child.close()


def frame(value, *, extended=False):
    body = bytes(ForkingPickler.dumps(value))
    header = (
        struct.pack("!iQ", -1, len(body)) if extended else struct.pack("!i", len(body))
    )
    return header + body


def test_connection_round_trip_and_bidirectional_blocking_mode(channel):
    parent, child, receiver = channel
    value = ("done", (3, 0.1, [{"unicode": "snow: 雪", "bytes": b"\x00\xff"}]))
    child.send(value)
    assert list(receiver.receive()) == [value]
    assert list(receiver.receive()) == []
    assert os.get_blocking(parent.fileno())
    parent.send([4, 5])
    assert child.recv() == [4, 5]


@pytest.mark.parametrize("extended", [False, True])
def test_fragmented_header_and_body_then_coalesced_frames(channel, extended):
    _, child, receiver = channel
    value = ("result", {"nested": [1, 2, 3]})
    packet = frame(value, extended=extended)
    for byte in packet[:-1]:
        os.write(child.fileno(), bytes([byte]))
        assert list(receiver.receive()) == []
    os.write(child.fileno(), packet[-1:] + frame(None) + frame("next", extended=True))
    assert list(receiver.receive()) == [value, None, "next"]
    assert receiver.incomplete is None


def test_huge_advertised_body_only_stores_received_bytes(channel):
    _, child, receiver = channel
    os.write(child.fileno(), struct.pack("!iQ", -1, sys.maxsize) + b"abc")
    assert list(receiver.receive()) == []
    assert (
        receiver.incomplete
        == f"incomplete worker frame: received 3 of {sys.maxsize} payload bytes"
    )
    assert len(receiver._buffer) == 3


@pytest.mark.parametrize(
    "packet,fragment",
    [
        (b"\x00", "header bytes before EOF"),
        (struct.pack("!i", -1) + b"\x00", "header bytes before EOF"),
        (struct.pack("!i", 100) + b"abc", "payload bytes before EOF"),
        (struct.pack("!i", -2), "invalid worker frame length"),
        (struct.pack("!iQ", -1, 2**64 - 1), "exceeds platform limit"),
        (struct.pack("!i", 4) + b"oops", "invalid pickle"),
    ],
)
def test_malformed_or_truncated_frames_are_explicit(channel, packet, fragment):
    _, child, receiver = channel
    os.write(child.fileno(), packet)
    child.close()
    with pytest.raises(TransportError, match=fragment):
        list(receiver.receive())


def test_complete_message_before_clean_eof_is_delivered(channel):
    _, child, receiver = channel
    child.send(("finished", []))
    child.close()
    assert list(receiver.receive()) == [("finished", [])]
    assert receiver.eof
    assert receiver.incomplete is None


@pytest.mark.parametrize(
    "message",
    [
        None,
        (),
        ("unknown", None),
        ("done", ()),
        ("done", (0, 1, None)),
        ("finished", None),
    ],
)
def test_invalid_worker_protocol_is_transport_error(message):
    with pytest.raises(TransportError):
        _message_parts(message)


class LargeReport:
    def initialize(self, worker_id):
        pass

    def execute(self, index, next_index, emit):
        emit({"text": "x" * (2 * 1024 * 1024 + 1)})

    def finalize(self, emit):
        pass


def test_large_normal_report_spans_receive_budgets():

    events = []
    result = execute_parallel(LargeReport(), [0], workers=1, on_event=events.append)
    assert result.completed == {0}
    assert not result.crashes
    assert len(events) == 1
    assert events[0]["text"] == "x" * (2 * 1024 * 1024 + 1)


def malformed_worker(connection, worker_id, adapter, initial):
    os.setsid()
    connection.send(("unknown", None))
    connection.close()
    os._exit(0)


def test_invalid_worker_protocol_is_reported_as_crash(monkeypatch):

    monkeypatch.setattr(executor, "_worker", malformed_worker)
    result = execute_parallel(None, [0], workers=1, on_event=lambda event: None)
    assert result.completed == set()
    assert len(result.crashes) == 1
    assert "unknown worker message" in result.crashes[0]["traceback"]
    assert result.crashes[0]["uncompleted"] == [0]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork worker backend")
def test_mid_packet_worker_death_with_inherited_socket_is_bounded(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    stopped = tmp_path / "descendant.stopped"
    source = textwrap.dedent(f"""
        import json
        import multiprocessing.connection
        import os
        from pathlib import Path
        import signal
        import struct
        import time
        from bistra.executor import execute_parallel
        pidfile = Path({str(pidfile)!r})
        stopped = Path({str(stopped)!r})
        class PartialPacket:
            def initialize(self, worker_id):
                original = multiprocessing.connection.Connection._send_bytes
                def send_bytes(connection, body):
                    if len(body) > 16384:
                        connection._send(struct.pack("!i", len(body)))
                        connection._send(body[:8192])
                        os._exit(23)
                    return original(connection, body)
                multiprocessing.connection.Connection._send_bytes = send_bytes
            def execute(self, index, next_index, emit):
                if os.fork() == 0:
                    def terminate(signum, frame):
                        stopped.write_text("terminated")
                        os._exit(0)
                    signal.signal(signal.SIGTERM, terminate)
                    pidfile.write_text(str(os.getpid()))
                    while True:
                        signal.pause()
                deadline = time.monotonic() + 3
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                emit({{"text": "x" * 100000}})
            def finalize(self, emit):
                pass
        if __name__ == "__main__":
            result = execute_parallel(PartialPacket(), [0], workers=1, on_event=lambda event: None)
            print(json.dumps({{"crashes": result.crashes, "completed": sorted(result.completed),
                               "stopped": stopped.exists()}}))
    """)
    env = dict(os.environ)
    script = tmp_path / "executor_probe.py"
    script.write_text(source)
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(result.stdout)
        assert data["completed"] == []
        assert len(data["crashes"]) == 1
        crash = data["crashes"][0]
        assert crash["exitcode"] == 23
        assert crash["uncompleted"] == [0]
        assert "incomplete worker frame: received 8192 of" in crash["traceback"]
        assert data["stopped"] is True
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

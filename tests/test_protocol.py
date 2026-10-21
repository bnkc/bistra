import errno
import pickle
import socket
import struct
import time

import pytest

from bistra.protocol import TransportError, _Channel


def frame(message: dict) -> bytes:
    payload = pickle.dumps(message)
    return struct.pack("!Q", len(payload)) + payload


def test_fragmented_message_is_delivered_once():
    sender, receiver = socket.socketpair()
    channel = _Channel(receiver)
    message = {"kind": "run", "tests": (1, 3, 5)}
    packet = frame(message)
    try:
        for piece in (packet[:3], packet[3:8], packet[8:-1]):
            sender.sendall(piece)
            assert channel.receive() == []
        sender.sendall(packet[-1:])
        assert channel.receive() == [message]
        assert channel.receive() == []
        sender.shutdown(socket.SHUT_WR)
        assert channel.receive() == []
        assert channel.eof
    finally:
        sender.close()
        channel.close()


@pytest.mark.parametrize("disconnect", ["eof", "reset"])
@pytest.mark.parametrize("tail", [None, "header", "payload"])
def test_disconnect_preserves_messages_and_rejects_partial_frames(monkeypatch, disconnect, tail):
    sender, receiver = socket.socketpair()
    channel = _Channel(receiver)
    messages = [
        {"kind": "need"},
        {"kind": "finished", "status": 1, "workeroutput": {}, "events": [], "error": None},
    ]
    packet = frame({"kind": "run", "tests": (4, 5)})
    truncated = {None: b"", "header": packet[:3], "payload": packet[:-1]}[tail]
    recv = socket.socket.recv

    def receive(connection, *args):
        data = recv(connection, *args)
        if connection is receiver and not data and disconnect == "reset":
            raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        return data

    monkeypatch.setattr(socket.socket, "recv", receive)
    try:
        sender.sendall(b"".join(frame(message) for message in messages) + truncated)
        sender.shutdown(socket.SHUT_WR)
        assert channel.receive() == messages
        assert channel.eof
        if tail is None:
            assert channel.error is None
            assert channel.receive() == []
        else:
            with pytest.raises(TransportError, match="incomplete message.*before EOF"):
                channel.receive()
    finally:
        sender.close()
        channel.close()


def test_backpressure_preserves_queued_messages_in_order():
    sender, receiver = socket.socketpair()
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    outgoing, incoming = _Channel(sender), _Channel(receiver)
    report = {"kind": "report", "data": {"longrepr": "\n".join(map(str, range(500_000)))}}
    following = {"kind": "need"}
    try:
        outgoing.queue(report)
        outgoing.flush()
        assert outgoing.pending
        outgoing.queue(following)
        messages = []
        deadline = time.monotonic() + 10
        while outgoing.pending or len(messages) < 2:
            assert time.monotonic() < deadline
            messages.extend(incoming.receive())
            outgoing.flush()
        assert messages == [report, following]
        assert not outgoing.pending
        assert incoming.receive() == []
    finally:
        outgoing.close()
        incoming.close()

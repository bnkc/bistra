"""Read multiprocessing Connection frames incrementally on POSIX sockets.

Connection.poll() can report a partial packet. Calling recv() then could hang
if the worker dies while a descendant keeps its socket open. This reader never
waits for the rest of a frame.
"""

import os
import socket
import struct
import sys
from collections.abc import Iterator
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler


class TransportError(Exception):
    """A malformed or truncated worker message."""


class FramedReceiver:
    """Read a duplex Connection without making its sends nonblocking.

    MSG_DONTWAIT affects one read. O_NONBLOCK would also affect Connection.send,
    even when set on a duplicated descriptor.
    """

    def __init__(self, connection: Connection) -> None:
        self._socket = socket.socket(fileno=os.dup(connection.fileno()))
        self._buffer = bytearray()
        self._expected: int | None = None
        self.eof = False

    def close(self) -> None:
        self._socket.close()

    @property
    def incomplete(self) -> str | None:
        if self._expected is not None:
            return f"incomplete worker frame: received {len(self._buffer)} of {self._expected} payload bytes"
        if self._buffer:
            needed = 12 if len(self._buffer) >= 4 else 4
            return f"incomplete worker frame: received {len(self._buffer)} of {needed} header bytes"
        return None

    def receive(self, *, byte_budget: int = 1024 * 1024) -> Iterator[object]:
        """Yield complete messages, reading at most byte_budget new bytes.

        The buffer grows as bytes arrive, regardless of the claimed frame size.
        The budget lets the parent keep checking for dead workers while data arrives.
        """
        received = 0
        while True:
            while True:
                if self._expected is None:
                    if len(self._buffer) < 4:
                        break
                    (size,) = struct.unpack_from("!i", self._buffer)
                    header_size = 4
                    if size == -1:
                        if len(self._buffer) < 12:
                            break
                        (size,) = struct.unpack_from("!Q", self._buffer, 4)
                        header_size = 12
                    elif size < 0:
                        raise TransportError(f"invalid worker frame length: {size}")
                    if size > sys.maxsize:
                        raise TransportError(f"worker frame length exceeds platform limit: {size}")
                    del self._buffer[:header_size]
                    self._expected = size
                if len(self._buffer) < self._expected:
                    break
                # Decode the complete packet without copying it. Release the
                # view before resizing the buffer.
                payload = memoryview(self._buffer)[: self._expected]
                try:
                    message = ForkingPickler.loads(payload)
                except Exception as error:
                    raise TransportError(f"invalid pickle in worker frame: {error}") from error
                finally:
                    payload.release()
                del self._buffer[: self._expected]
                self._expected = None
                yield message
            if self.eof or received >= byte_budget:
                return
            try:
                chunk = self._socket.recv(min(65536, byte_budget - received), socket.MSG_DONTWAIT)
            except BlockingIOError:
                return
            except OSError as error:
                raise TransportError(f"cannot read worker socket: {error}") from error
            if not chunk:
                self.eof = True
                if self.incomplete:
                    raise TransportError(self.incomplete + " before EOF")
                return
            self._buffer.extend(chunk)
            received += len(chunk)

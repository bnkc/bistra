"""Incremental reception of multiprocessing Connection frames on POSIX sockets.

Connection.poll() only promises some bytes, not a complete pickle. Connection.recv()
can therefore block forever if a worker dies midway through a packet while a
descendant retains its socket. This reader never waits for the rest of a frame.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
from multiprocessing.reduction import ForkingPickler


class TransportError(Exception):
    """A malformed or truncated worker message."""


class FramedReceiver:
    """Read a duplex Connection without changing its blocking send behavior.

    MSG_DONTWAIT applies to individual reads. Setting O_NONBLOCK on even a
    duplicated descriptor would also make Connection.send nonblocking.
    """

    def __init__(self, connection):
        self._socket = socket.socket(fileno=os.dup(connection.fileno()))
        self._buffer = bytearray()
        self._expected = None
        self.eof = False

    def close(self):
        self._socket.close()

    @property
    def incomplete(self):
        if self._expected is not None:
            return (
                f"incomplete worker frame: received {len(self._buffer)} of "
                f"{self._expected} payload bytes"
            )
        if self._buffer:
            needed = 12 if len(self._buffer) >= 4 else 4
            return (
                f"incomplete worker frame: received {len(self._buffer)} of "
                f"{needed} header bytes"
            )
        return None

    def receive(self, *, byte_budget=1024 * 1024):
        """Yield complete messages, reading at most byte_budget new bytes.

        Storage grows only with bytes actually received, never with a claimed
        frame length. The bounded read budget also lets the parent poll waitpid
        while a worker or its descendants are producing output.
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
                        raise TransportError(
                            f"worker frame length exceeds platform limit: {size}"
                        )
                    del self._buffer[:header_size]
                    self._expected = size
                if len(self._buffer) < self._expected:
                    break
                # Decode only after a whole packet is present. A bounded view
                # avoids an extra copy; release it before resizing the buffer.
                payload = memoryview(self._buffer)[: self._expected]
                try:
                    message = ForkingPickler.loads(payload)
                except Exception as error:
                    raise TransportError(
                        f"invalid pickle in worker frame: {error}"
                    ) from error
                finally:
                    payload.release()
                del self._buffer[: self._expected]
                self._expected = None
                yield message
            if self.eof or received >= byte_budget:
                return
            try:
                chunk = self._socket.recv(
                    min(65536, byte_budget - received), socket.MSG_DONTWAIT
                )
            except BlockingIOError:
                return
            except InterruptedError:
                continue
            except OSError as error:
                raise TransportError(f"cannot read worker socket: {error}") from error
            if not chunk:
                self.eof = True
                if self.incomplete:
                    raise TransportError(self.incomplete + " before EOF")
                return
            self._buffer.extend(chunk)
            received += len(chunk)

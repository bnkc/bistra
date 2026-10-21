import pickle
import selectors
import socket
import struct
import sys
from collections import deque
from typing import Any, Literal, NotRequired, TypeAlias, TypedDict, cast

from .scheduling import Collection


class Report(TypedDict):
    type: Literal["report", "collectreport"]
    data: dict[str, Any]


class Log(TypedDict):
    type: Literal["logfinish"]
    nodeid: str
    location: tuple[str, int | None, str]


class Warning(TypedDict):
    type: Literal["warning"]
    data: dict[str, Any]
    when: Literal["config", "collect", "runtest"]
    nodeid: str
    location: tuple[str, int, str] | None


class ControlEvent(TypedDict):
    type: Literal["control"]
    kind: Literal["exit", "interrupt", "fail", "stop"]
    message: str
    returncode: NotRequired[int | None]


ReportEvent: TypeAlias = Report | Log | Warning
Event: TypeAlias = ReportEvent | ControlEvent


class Bootstrap(TypedDict):
    kind: Literal["bootstrap"]
    args: list[str]
    plugins: list[str]
    workerinput: dict[str, Any]
    path: list[str]
    base: str
    log: str


class Run(TypedDict):
    kind: Literal["run"]
    tests: tuple[int, ...]


class End(TypedDict):
    kind: Literal["finish", "stop"]


Command: TypeAlias = Run | End


class Ready(TypedDict):
    kind: Literal["ready"]
    collection: Collection
    events: list[Event]
    deselected: int


class Need(TypedDict):
    kind: Literal["need"]


class Started(TypedDict):
    kind: Literal["started"]
    index: int
    location: tuple[str, int | None, str]


class Reported(TypedDict):
    kind: Literal["report"]
    data: dict[str, Any]


class Completed(TypedDict):
    kind: Literal["completed"]
    index: int
    elapsed: float
    events: list[Event]


class Finished(TypedDict):
    kind: Literal["finished"]
    status: int
    workeroutput: dict[str, Any]
    events: list[Event]
    error: str | None


WorkerMessage: TypeAlias = Ready | Need | Started | Reported | Completed | Finished
Message: TypeAlias = Bootstrap | Command | WorkerMessage
_KINDS = {"bootstrap", "run", "finish", "stop", "ready", "need", "started", "report", "completed", "finished"}
_HEADER = struct.Struct("!Q")
_BUDGET = 1024 * 1024


class TransportError(ConnectionError):
    """A closed connection or malformed worker message."""


class _Channel:
    """send() and wait() block and are only for workers.

    Each I/O pass has a byte budget to keep a busy worker from starving others.
    """

    def __init__(self, connection: socket.socket) -> None:
        self._socket = connection
        self._socket.setblocking(False)
        self._buffer = bytearray()
        self._expected: int | None = None
        self._outgoing: deque[memoryview] = deque()
        self._incoming: deque[Message] = deque()
        self._error: TransportError | None = None
        self.eof = False

    def fileno(self) -> int:
        return self._socket.fileno()

    @property
    def pending(self) -> bool:
        return bool(self._outgoing)

    @property
    def error(self) -> str | None:
        return str(self._error) if self._error is not None else None

    @property
    def incomplete(self) -> str | None:
        if self._expected is not None:
            return f"incomplete message: received {len(self._buffer)} of {self._expected} bytes"
        if self._buffer:
            return f"incomplete message header: received {len(self._buffer)} of {_HEADER.size} bytes"
        return None

    def queue(self, message: Message) -> None:
        payload = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
        self._outgoing.extend((memoryview(_HEADER.pack(len(payload))), memoryview(payload)))

    def flush(self) -> None:
        remaining = _BUDGET
        while self._outgoing and remaining:
            packet = self._outgoing[0]
            try:
                written = self._socket.send(packet[:remaining])
            except BlockingIOError:
                return
            except OSError as error:
                raise TransportError(f"cannot send worker message: {error}") from error
            if not written:
                raise TransportError("worker connection closed during send")
            remaining -= written
            self._outgoing.popleft()
            if written < len(packet):
                self._outgoing.appendleft(packet[written:])

    def receive(self) -> list[Message]:
        messages = list(self._incoming)
        self._incoming.clear()
        if self._error is not None:
            if messages:
                return messages
            raise self._error
        remaining = _BUDGET
        try:
            while True:
                while self._buffer:
                    if self._expected is None:
                        if len(self._buffer) < _HEADER.size:
                            break
                        (self._expected,) = _HEADER.unpack_from(self._buffer)
                        del self._buffer[: _HEADER.size]
                        if self._expected > sys.maxsize:
                            raise TransportError("worker message exceeds platform size limit")
                    if len(self._buffer) < self._expected:
                        break
                    payload = memoryview(self._buffer)[: self._expected]
                    try:
                        message = pickle.loads(payload)
                    except Exception as error:
                        raise TransportError(f"cannot decode worker message: {error}") from error
                    finally:
                        payload.release()
                    del self._buffer[: self._expected]
                    self._expected = None
                    if (
                        not isinstance(message, dict)
                        or not isinstance(message.get("kind"), str)
                        or message["kind"] not in _KINDS
                    ):
                        raise TransportError("invalid worker message envelope")
                    messages.append(cast(Message, message))
                if self.eof or not remaining:
                    return messages
                try:
                    chunk = self._socket.recv(min(65536, remaining))
                except BlockingIOError:
                    return messages
                except ConnectionResetError:
                    # Linux resets a socket closed with unread controller commands.
                    chunk = b""
                except OSError as error:
                    raise TransportError(f"cannot read worker message: {error}") from error
                if not chunk:
                    self.eof = True
                    if self.incomplete:
                        raise TransportError(self.incomplete + " before EOF")
                    return messages
                remaining -= len(chunk)
                self._buffer.extend(chunk)
        except TransportError as error:
            # Deliver complete preceding messages before reporting a broken tail.
            self._error = error
            if messages:
                return messages
            raise

    def wait(self) -> Message:
        messages = self.receive()
        if not messages and not self.eof:
            with selectors.DefaultSelector() as selector:
                selector.register(self, selectors.EVENT_READ)
                while not messages and not self.eof:
                    selector.select()
                    messages = self.receive()
        if not messages:
            raise TransportError("controller connection closed")
        self._incoming.extend(messages[1:])
        return messages[0]

    def send(self, message: Message) -> None:
        self.queue(message)
        self.flush()
        if self.pending:
            with selectors.DefaultSelector() as selector:
                selector.register(self, selectors.EVENT_WRITE)
                while self.pending:
                    selector.select()
                    self.flush()

    def close(self) -> None:
        self._socket.close()
        self._outgoing.clear()

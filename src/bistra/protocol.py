"""Types shared by the executor and pytest worker adapters."""

from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Literal, NotRequired, Protocol, TypeAlias, TypedDict


class EventContext(TypedDict, total=False):
    worker_id: int
    index: int | None


class ReportEvent(EventContext):
    type: Literal["report"]
    data: dict[str, Any]


class LogStartEvent(EventContext):
    type: Literal["logstart"]
    nodeid: str
    location: tuple[str, int | None, str]


class LogFinishEvent(EventContext):
    type: Literal["logfinish"]
    nodeid: str
    location: tuple[str, int | None, str]


class WarningEvent(EventContext):
    type: Literal["warning"]
    data: dict[str, Any]
    when: Literal["config", "collect", "runtest"]
    nodeid: str
    location: tuple[str, int, str] | None


class ControlEvent(EventContext):
    type: Literal["control"]
    kind: Literal["exit", "interrupt", "fail", "stop"]
    message: str
    returncode: NotRequired[int | None]


Event: TypeAlias = ReportEvent | LogStartEvent | LogFinishEvent | WarningEvent | ControlEvent
Emit: TypeAlias = Callable[[Event], None]
OnEvent: TypeAlias = Callable[[Event], bool | None]
StartWorker: TypeAlias = Callable[[int, list[int]], tuple[BaseProcess, Connection]]


class WorkerAdapter(Protocol):
    def initialize(self, worker_id: int) -> None: ...

    def execute(self, index: int, next_index: int | None, emit: Emit) -> None: ...

    def finalize(self, emit: Emit) -> None: ...


class WorkerFailure(TypedDict):
    index: int | None
    traceback: str
    worker_id: NotRequired[int]
    exitcode: NotRequired[int | None]
    uncompleted: NotRequired[list[int]]
    events: NotRequired[list[Event]]


WorkerMessage: TypeAlias = (
    tuple[Literal["need"], None]
    | tuple[Literal["started"], int]
    | tuple[Literal["done"], tuple[int, float, list[Event]]]
    | tuple[Literal["finished"], list[Event]]
    | tuple[Literal["crash", "stopped"], WorkerFailure]
)

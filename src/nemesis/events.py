from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import List, Tuple, Optional


class Event(ABC):
    ...


@dataclass(frozen=True)
class RawData(Event):
    data: bytes
    address: Optional[Tuple[str, int]] = None


@dataclass(frozen=True)
class Closed(Event):
    pass


@dataclass(frozen=True)
class Updated(Event):
    idle: bool


@dataclass(frozen=True)
class StreamEvent(Event):
    stream_id: int


@dataclass(frozen=True)
class Request(StreamEvent):
    headers: List[Tuple[bytes, bytes]]
    http_version: str
    method: str
    raw_path: bytes


@dataclass(frozen=True)
class Body(StreamEvent):
    data: bytes


@dataclass(frozen=True)
class EndBody(StreamEvent):
    pass


@dataclass(frozen=True)
class Data(StreamEvent):
    data: bytes


@dataclass(frozen=True)
class EndData(StreamEvent):
    pass


@dataclass(frozen=True)
class Response(StreamEvent):
    headers: List[Tuple[bytes, bytes]]
    status_code: int


@dataclass(frozen=True)
class StreamClosed(StreamEvent):
    pass

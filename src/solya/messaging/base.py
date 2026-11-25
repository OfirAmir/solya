from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass
class OutboundMessage:
    recipient: str
    kind: Literal["report", "fault", "comm_loss", "welcome", "reply"]
    text: str
    image_png: bytes | None
    dedupe_key: str
    template: str | None = None
    template_vars: dict | None = None


@dataclass(frozen=True)
class Ok:
    pass


@dataclass(frozen=True)
class RetryAfter:
    seconds: float


@dataclass(frozen=True)
class Blocked:
    pass


@dataclass(frozen=True)
class Invalid:
    reason: str


SendResult = Ok | RetryAfter | Blocked | Invalid


class Channel(Protocol):
    name: str
    caption_limit: int

    async def send(self, msg: OutboundMessage) -> SendResult: ...

    def can_send_freeform(self, recipient: str) -> bool: ...

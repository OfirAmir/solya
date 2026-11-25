import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from solya.clock import Clock
from solya.db import session_scope
from solya.messaging.base import Blocked, Channel, Invalid, Ok, OutboundMessage, RetryAfter, SendResult
from solya.models import OutboxEntry, Owner

log = logging.getLogger(__name__)

MAX_RETRY_SLEEP = 60.0
# What str() makes of an unlinked owner's chat id; never a real recipient.
_NO_RECIPIENT = {"", "None"}


class Outbox:
    """Records every send under a unique dedupe_key so restarts never double-send."""

    def __init__(self, session_factory: sessionmaker, clock: Clock) -> None:
        self._sf = session_factory
        self._clock = clock

    async def deliver(
        self,
        channel: Channel,
        msg: OutboundMessage,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> SendResult:
        if msg.recipient in _NO_RECIPIENT:
            # Defence in depth: callers skip owners with no chat before this point. Nothing
            # is recorded, so the message can still go out once the chat is linked.
            log.warning("skip_unlinked: %s has no recipient; not sent", msg.dedupe_key)
            return Invalid("no recipient")
        with session_scope(self._sf) as s:
            entry = s.get(OutboxEntry, msg.dedupe_key)
            if entry is not None and entry.status == "sent":
                return Ok()
            if entry is None:
                s.add(
                    OutboxEntry(
                        dedupe_key=msg.dedupe_key,
                        channel=channel.name,
                        recipient=msg.recipient,
                        kind=msg.kind,
                        status="pending",
                        attempts=0,
                    )
                )
            else:
                entry.status = "pending"
                entry.error = None

        result = await channel.send(msg)
        attempts = 1
        if isinstance(result, RetryAfter):
            await sleep(min(result.seconds, MAX_RETRY_SLEEP))
            result = await channel.send(msg)
            attempts = 2

        self._record(channel, msg, result, attempts)
        return result

    def _record(
        self, channel: Channel, msg: OutboundMessage, result: SendResult, attempts: int
    ) -> None:
        with session_scope(self._sf) as s:
            entry = s.get(OutboxEntry, msg.dedupe_key)
            entry.attempts += attempts
            match result:
                case Ok():
                    entry.status = "sent"
                    entry.sent_at = self._clock.now()
                case Blocked():
                    entry.status = "blocked"
                    self._deactivate_owner(s, msg.recipient)
                case Invalid(reason=reason):
                    entry.status = "invalid"
                    entry.error = reason
                case RetryAfter(seconds=seconds):
                    entry.status = "retry"
                    entry.error = f"rate limited, retry after {seconds}s"

    @staticmethod
    def _deactivate_owner(s, recipient: str) -> None:
        try:
            chat_id = int(recipient)
        except ValueError:
            return
        owner = s.scalar(select(Owner).where(Owner.telegram_chat_id == chat_id))
        if owner is not None:
            owner.active = False

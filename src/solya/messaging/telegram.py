from datetime import timedelta

from telegram import Bot
from telegram.error import BadRequest, Forbidden, RetryAfter as TgRetryAfter

from solya.messaging.base import Blocked, Invalid, Ok, OutboundMessage, RetryAfter, SendResult

MAX_TEXT = 4096


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line boundaries; a single over-long line is hard-split."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = line
    if current:
        parts.append(current)
    return parts


class TelegramChannel:
    name = "telegram"
    caption_limit = 1024

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    def can_send_freeform(self, recipient: str) -> bool:
        return True

    async def send(self, msg: OutboundMessage) -> SendResult:
        chat_id = int(msg.recipient)
        try:
            # msg.text is already final (post rtl_text), so this length is what Telegram counts.
            if msg.image_png is not None and msg.text and len(msg.text) <= self.caption_limit:
                await self._bot.send_photo(chat_id=chat_id, photo=msg.image_png, caption=msg.text)
            else:
                if msg.image_png is not None:
                    await self._bot.send_photo(chat_id=chat_id, photo=msg.image_png)
                if msg.text:
                    for part in split_text(msg.text):
                        await self._bot.send_message(chat_id=chat_id, text=part)
        except TgRetryAfter as e:
            ra = e.retry_after
            seconds = ra.total_seconds() if isinstance(ra, timedelta) else float(ra)
            return RetryAfter(seconds=seconds)
        except Forbidden:
            return Blocked()
        except BadRequest as e:
            return Invalid(reason=str(e))
        return Ok()

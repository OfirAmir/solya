from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden
from telegram.error import RetryAfter as TgRetryAfter

from solya.db import session_scope
from solya.messaging.base import Blocked, Invalid, Ok, OutboundMessage, RetryAfter
from solya.messaging.outbox import Outbox
from solya.messaging.telegram import TelegramChannel
from solya.models import OutboxEntry, Owner

KEY = "report:1:2026-11-24"


def make_msg(text="שלום", image=None, key=KEY, kind="report") -> OutboundMessage:
    return OutboundMessage(
        recipient="555", kind=kind, text=text, image_png=image, dedupe_key=key
    )


def add_owners(db, clock):
    with session_scope(db) as s:
        s.add(Owner(display_name="a", telegram_chat_id=555, created_at=clock.now()))
        s.add(Owner(display_name="b", telegram_chat_id=777, created_at=clock.now()))


async def no_sleep(_):
    return None


async def test_outbox_dedupe(db, clock, fake_channel):
    outbox = Outbox(db, clock)
    assert isinstance(await outbox.deliver(fake_channel, make_msg()), Ok)
    assert isinstance(await outbox.deliver(fake_channel, make_msg()), Ok)
    assert len(fake_channel.sent) == 1
    with session_scope(db) as s:
        e = s.get(OutboxEntry, KEY)
        assert e.status == "sent"
        assert e.sent_at == clock.now()
        assert e.channel == "fake"


async def test_blocked_marks_inactive(db, clock, fake_channel):
    add_owners(db, clock)
    fake_channel.result = Blocked()
    res = await Outbox(db, clock).deliver(fake_channel, make_msg())
    assert isinstance(res, Blocked)
    assert len(fake_channel.sent) == 1  # no retries
    with session_scope(db) as s:
        assert s.get(OutboxEntry, KEY).status == "blocked"
        owners = {o.telegram_chat_id: o.active for o in s.query(Owner)}
        assert owners == {555: False, 777: True}


async def test_retry_after_retries_once(db, clock, fake_channel):
    fake_channel.result = [RetryAfter(seconds=500), Ok()]
    slept = []

    async def sleep(s):
        slept.append(s)

    res = await Outbox(db, clock).deliver(fake_channel, make_msg(), sleep=sleep)
    assert isinstance(res, Ok)
    assert slept == [60]
    assert len(fake_channel.sent) == 2
    with session_scope(db) as s:
        e = s.get(OutboxEntry, KEY)
        assert e.status == "sent" and e.attempts == 2


async def test_retry_after_twice_gives_up(db, clock, fake_channel):
    fake_channel.result = RetryAfter(seconds=2)
    res = await Outbox(db, clock).deliver(fake_channel, make_msg(), sleep=no_sleep)
    assert isinstance(res, RetryAfter)
    assert len(fake_channel.sent) == 2
    with session_scope(db) as s:
        assert s.get(OutboxEntry, KEY).status == "retry"


async def test_failed_entry_can_be_redelivered(db, clock, fake_channel):
    fake_channel.result = [Invalid("bad"), Ok()]
    outbox = Outbox(db, clock)
    assert isinstance(await outbox.deliver(fake_channel, make_msg()), Invalid)
    assert isinstance(await outbox.deliver(fake_channel, make_msg()), Ok)
    assert len(fake_channel.sent) == 2


async def test_telegram_photo_then_text():
    bot = AsyncMock()
    res = await TelegramChannel(bot).send(make_msg(text="hi", image=b"png"))
    assert isinstance(res, Ok)
    assert [c[0] for c in bot.method_calls] == ["send_photo", "send_message"]
    bot.send_photo.assert_awaited_once_with(chat_id=555, photo=b"png")
    assert bot.send_message.await_args.kwargs == {"chat_id": 555, "text": "hi"}


async def test_telegram_photo_ok_text_fails_returns_text_failure():
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("blocked")
    res = await TelegramChannel(bot).send(make_msg(image=b"png"))
    assert isinstance(res, Blocked)
    bot.send_photo.assert_awaited_once()


async def test_telegram_splits_long_text():
    bot = AsyncMock()
    text = "\n".join(["א" * 99] * 100)  # 9999 chars
    res = await TelegramChannel(bot).send(make_msg(text=text, kind="reply"))
    assert isinstance(res, Ok)
    parts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert len(parts) == 3
    assert all(len(p) <= 4096 for p in parts)
    assert "\n".join(parts) == text


async def test_telegram_splits_overlong_single_line():
    bot = AsyncMock()
    text = "x" * 5000
    await TelegramChannel(bot).send(make_msg(text=text))
    parts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert [len(p) for p in parts] == [4096, 904]


async def test_telegram_forbidden_maps_blocked():
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("blocked")
    assert isinstance(await TelegramChannel(bot).send(make_msg()), Blocked)


async def test_telegram_bad_request_maps_invalid():
    bot = AsyncMock()
    bot.send_message.side_effect = BadRequest("chat not found")
    res = await TelegramChannel(bot).send(make_msg())
    assert isinstance(res, Invalid) and "chat not found" in res.reason


@pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")
@pytest.mark.parametrize("ra", [7, timedelta(seconds=7)])
async def test_telegram_retry_after_int_or_timedelta(ra):
    bot = AsyncMock()
    bot.send_message.side_effect = TgRetryAfter(ra)
    res = await TelegramChannel(bot).send(make_msg())
    assert res == RetryAfter(seconds=7.0)


def test_telegram_channel_props():
    ch = TelegramChannel(AsyncMock())
    assert ch.name == "telegram" and ch.caption_limit == 1024
    assert ch.can_send_freeform("555") is True

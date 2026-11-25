import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import inspect, text

from solya.bot import build_bot_application
from solya.models import DailyMetric, FaultRecord, LlmUsage, Owner, Site
from solya.services import ai_answers
from solya.services import onboarding as ob
from solya.text.he import AI_BUDGET_USED, AI_UNAVAILABLE, HELP

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
URL = "https://llm.example/v1/chat/completions"
KEY = "sk-secret-key-123"
EMAIL = "dana@example.com"
PHONE = "0501234567"


@pytest.fixture
def ai_settings(settings):
    return settings.model_copy(
        update={
            "llm_enabled": True,
            "llm_base_url": "https://llm.example/v1",
            "llm_api_key": SecretStr(KEY),
            "llm_model": "test/model",
        }
    )


@pytest.fixture
def owner_id(db):
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        o = ob.redeem_invite(s, tok, 5, "דנה", NOW)
        o.email = EMAIL
        o.phone = PHONE
        s.add(Site(se_site_id=1, owner_id=o.id, portal_name="P", tz="Asia/Jerusalem", created_at=NOW))
        s.add(
            DailyMetric(
                site_id=1,
                local_date=date(2026, 11, 23),
                production_wh=316990.0,
                income_ils=Decimal("120.50"),
                comm_state="ok",
                as_of=NOW,
                computed_at=NOW,
            )
        )
        s.add(
            FaultRecord(
                site_id=1, key="k", code="E7", serial="SN123", first_seen=NOW, source="alerts"
            )
        )
        s.commit()
        return o.id


def _reply(content, pt=100, ct=20):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct},
        },
    )


async def _ask(settings, db, owner_id, q="מה קורה?"):
    async with httpx.AsyncClient() as http:
        return await ai_answers.answer(http, settings, db, owner_id, q, NOW)


@respx.mock
async def test_ai_disabled_returns_help(settings, db, owner_id):
    route = respx.post(URL).mock(return_value=_reply("x"))
    assert await _ask(settings, db, owner_id) == HELP
    assert not route.called


@respx.mock
async def test_ai_no_key_returns_help(ai_settings, db, owner_id):
    route = respx.post(URL).mock(return_value=_reply("x"))
    s = ai_settings.model_copy(update={"llm_api_key": None})
    assert await _ask(s, db, owner_id) == HELP
    assert not route.called


@respx.mock
async def test_ai_answer_uses_context_only(ai_settings, db, owner_id):
    route = respx.post(URL).mock(return_value=_reply("אתמול הופקו 316.99 קוט״ש"))
    out = await _ask(ai_settings, db, owner_id, "כמה הפקתי?")
    assert "316.99" in out
    req = route.calls[0].request
    body = req.content.decode()
    for secret in (EMAIL, PHONE, "123456:test-bot-token"):
        assert secret not in body
    payload = json.loads(body)
    assert payload["max_tokens"] == 300
    assert payload["model"] == "test/model"
    assert "SN123" in body and "316.99" in body and "כמה הפקתי?" in body
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in body


@respx.mock
async def test_ai_numeric_guard_blocks_invented_number(ai_settings, db, owner_id):
    respx.post(URL).mock(return_value=_reply("הפקת 999 קוט״ש"))
    assert await _ask(ai_settings, db, owner_id) == HELP


@respx.mock
async def test_ai_numeric_guard_allows_rounded(ai_settings, db, owner_id):
    respx.post(URL).mock(return_value=_reply("הפקת 317 קוט״ש ב-23 בנובמבר 2026"))
    assert "317" in await _ask(ai_settings, db, owner_id)


def test_numeric_guard_separators():
    ctx = {"a": 1234.5, "d": "2026-11-24"}
    ok = ai_answers.numbers_grounded
    assert ok("1,234.5", ctx) and ok("1234,5", ctx) and ok("1,235", ctx)
    assert ok("24 ב-11 2026", ctx) and ok("ללא מספרים", ctx)
    assert not ok("1,234", ctx)  # thousands reading: 1234 is not 1234.5 rounded to 0/1/2 places
    assert not ok("12", ctx)


@respx.mock
async def test_ai_budget_exhausted(ai_settings, db, owner_id):
    with db() as s:
        s.add(LlmUsage(owner_id=owner_id, ts=NOW, input_tokens=40000, output_tokens=10000))
        s.commit()
    route = respx.post(URL).mock(return_value=_reply("x"))
    assert await _ask(ai_settings, db, owner_id) == AI_BUDGET_USED
    assert not route.called


@respx.mock
@pytest.mark.parametrize(
    "mock",
    [
        lambda: httpx.Response(503),
        lambda: httpx.ReadTimeout("slow"),
        lambda: httpx.Response(200, text="not json"),
    ],
)
async def test_ai_provider_error_fallback(ai_settings, db, owner_id, caplog, mock):
    m = mock()
    respx.post(URL).mock(side_effect=m if isinstance(m, Exception) else None, return_value=m if not isinstance(m, Exception) else None)
    with caplog.at_level(logging.DEBUG):
        assert await _ask(ai_settings, db, owner_id) == AI_UNAVAILABLE
    assert KEY not in caplog.text
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert not any(r.exc_info for r in caplog.records)


@respx.mock
async def test_ai_usage_logged(ai_settings, db, owner_id):
    respx.post(URL).mock(return_value=_reply("אין לי נתונים", pt=111, ct=22))
    await _ask(ai_settings, db, owner_id)
    with db() as s:
        rows = s.query(LlmUsage).all()
        assert [(r.owner_id, r.input_tokens, r.output_tokens) for r in rows] == [(owner_id, 111, 22)]


def _app(settings, clock, db):
    container = SimpleNamespace(settings=settings, session_factory=db, clock=clock, http=httpx.AsyncClient())
    app = build_bot_application("123456:test-bot-token", container)
    out = {}
    for h in app.handlers[0]:
        for c in getattr(h, "commands", ["__text__"]):
            out[c] = h.callback
    return out, container


def _upd(text_):
    msg = SimpleNamespace(text=text_, reply_text=AsyncMock())
    upd = SimpleNamespace(
        effective_chat=SimpleNamespace(id=5),
        effective_user=SimpleNamespace(first_name="דנה"),
        message=msg,
        effective_message=msg,
    )
    return upd, SimpleNamespace(args=[]), msg


@respx.mock
async def test_bot_text_uses_ai_only_on_help_route(ai_settings, clock, db, owner_id):
    route = respx.post(URL).mock(return_value=_reply("אין לי נתונים"))
    h, c = _app(ai_settings, clock, db)
    try:
        for q in ("מה קרה היום?", "אתמול", "התראות", "החודש"):
            upd, ctx, msg = _upd(q)
            await h["__text__"](upd, ctx)
        assert not route.called
        upd, ctx, msg = _upd("למה הגג שלי כחול?")
        await h["__text__"](upd, ctx)
        assert route.call_count == 1
        assert "אין לי נתונים" in msg.reply_text.call_args[0][0]
        upd, ctx, msg = _upd("")
        await h["help"](upd, ctx)
        assert route.call_count == 1
    finally:
        await c.http.aclose()


@respx.mock
async def test_bot_text_ai_off_by_default(settings, clock, db, owner_id):
    route = respx.post(URL).mock(return_value=_reply("x"))
    h, c = _app(settings, clock, db)
    try:
        upd, ctx, msg = _upd("למה הגג שלי כחול?")
        await h["__text__"](upd, ctx)
        assert not route.called
        assert "אני יכולה לענות" in msg.reply_text.call_args[0][0]
    finally:
        await c.http.aclose()


def _snap(db):
    skip = {"last_inbound_at", "acked_at"}
    with db() as s:
        out = {}
        for t in inspect(s.get_bind()).get_table_names():
            if t == "llm_usage":
                continue
            res = s.execute(text(f"select * from {t} order by 1"))
            cols = [k for k in res.keys() if k not in skip]
            out[t] = [tuple(r._mapping[k] for k in cols) for r in res]
        return out


@respx.mock
async def test_ai_cannot_change_state(ai_settings, clock, db, owner_id):
    respx.post(URL).mock(
        return_value=_reply("ביטלתי את המנוי, החזר ניתן ונפתחה קריאת שירות")
    )
    before = _snap(db)
    h, c = _app(ai_settings, clock, db)
    try:
        upd, ctx, msg = _upd("בטל את המנוי שלי ותחזיר לי כסף")
        await h["__text__"](upd, ctx)
    finally:
        await c.http.aclose()
    assert _snap(db) == before
    with db() as s:
        assert s.query(Owner).count() == 1

"""Telegram handlers: thin wrappers over the onboarding and answers services."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from solya.db import session_scope
from solya.rules.faults import ack_owner_faults
from solya.services import answers, onboarding
from solya.text.he import HELP, INVITE_OK, NOT_INVITED, connect_link
from solya.text.rtl import rtl_text

log = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _safe(fn: Handler) -> Handler:
    """Never let one bad update crash the application (R15)."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await fn(update, context)
        except Exception:
            log.exception("handler %s failed", fn.__name__)

    wrapper.__name__ = fn.__name__
    return wrapper


def build_bot_application(token: str, container) -> Application:
    app = ApplicationBuilder().token(token).build()

    async def reply(update: Update, text: str) -> None:
        await update.effective_message.reply_text(rtl_text(text))

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        now = container.clock.now()
        with session_scope(container.session_factory) as s:
            owner = onboarding.get_owner_by_chat(s, chat.id)
            if owner is None and context.args:
                owner = onboarding.redeem_invite(
                    s, context.args[0], chat.id, update.effective_user.first_name or "", now
                )
        await reply(update, INVITE_OK if owner is not None else NOT_INVITED)

    async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = container.clock.now()
        with session_scope(container.session_factory) as s:
            owner = onboarding.get_owner_by_chat(s, update.effective_chat.id)
            url = (
                onboarding.create_connect_request(s, owner.id, now, container.settings)
                if owner is not None and owner.active
                else None
            )
        await reply(update, connect_link(url) if url else NOT_INVITED)

    def answer_command(route: answers.Route) -> Handler:
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            await _answer(update, route, ack=False)

        handler.__name__ = f"cmd_{route}"
        return handler

    async def _answer(update: Update, route: answers.Route | None, ack: bool) -> None:
        now = container.clock.now()
        with session_scope(container.session_factory) as s:
            owner = onboarding.get_owner_by_chat(s, update.effective_chat.id)
            if owner is None:
                text = NOT_INVITED
            else:
                if ack:
                    ack_owner_faults(s, owner.id, now)
                    owner.last_inbound_at = now
                    route = answers.route_text(update.effective_message.text or "")
                text = answers.answer_for(route, s, owner, now)
        await reply(update, text)

    async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _answer(update, None, ack=True)

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, HELP)

    app.add_handler(CommandHandler("start", _safe(start)))
    app.add_handler(CommandHandler("connect", _safe(connect)))
    for route in ("today", "yesterday", "month", "alerts"):
        app.add_handler(CommandHandler(route, _safe(answer_command(route))))
    app.add_handler(CommandHandler("help", _safe(help_cmd)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _safe(text_message)))
    return app

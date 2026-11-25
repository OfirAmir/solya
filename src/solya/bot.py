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
from solya.services import ai_answers, answers, onboarding
from solya.text.he import (
    CHAT_BOUND_ELSEWHERE, HELP, INVITE_OK, LINKED_OK, NOT_INVITED, connect_link,
)
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

    def known_owner(s, update: Update, now):
        """R27: every update from a registered owner, commands included, is inbound
        bookkeeping: last_inbound_at, fault ack and reactivation (R16)."""
        owner = onboarding.get_owner_by_chat(s, update.effective_chat.id)
        if owner is not None:
            onboarding.record_inbound(s, owner, now)
        return owner

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        now = container.clock.now()
        linked_owner_id = None
        with session_scope(container.session_factory) as s:
            payload = context.args[0] if context.args else None
            outcome = onboarding.link_chat(s, payload, chat.id, now) if payload else "unknown"
            if outcome == "linked":
                linked_owner_id = onboarding.get_owner_by_chat(s, chat.id).id
                owner = True
            elif outcome == "bound_elsewhere":
                owner = None
            else:
                owner = known_owner(s, update, now)
                if owner is None and payload:
                    owner = onboarding.redeem_invite(
                        s, payload, chat.id, update.effective_user.first_name or "", now
                    )
        if outcome == "bound_elsewhere":
            await reply(update, CHAT_BOUND_ELSEWHERE)
            return
        if linked_owner_id is not None:
            await _after_link(update, linked_owner_id)
            return
        await reply(update, INVITE_OK if owner is not None else NOT_INVITED)

    async def _after_link(update: Update, owner_id: int) -> None:
        """The link is already committed. Plan tonight's report first (Q2), then reply,
        then the welcome pair; one failing step never skips the others (final review M2)."""
        try:
            container.scheduler.plan_owner(owner_id)
        except Exception:
            log.exception("could not plan today's jobs for linked owner %s", owner_id)
        try:
            await reply(update, LINKED_OK)
        except Exception:
            log.exception("link reply to owner %s failed", owner_id)
        try:
            await container.reporter.on_owner_linked(owner_id)
        except Exception:
            log.exception("link-time messages for owner %s failed", owner_id)

    async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = container.clock.now()
        with session_scope(container.session_factory) as s:
            owner = known_owner(s, update, now)
            url = (
                onboarding.create_connect_request(s, owner.id, now, container.settings)
                if owner is not None
                else None
            )
        await reply(update, connect_link(url) if url else NOT_INVITED)

    def answer_command(route: answers.Route) -> Handler:
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            await _answer(update, route)

        handler.__name__ = f"cmd_{route}"
        return handler

    async def _answer(update: Update, route: answers.Route | None) -> None:
        """``route`` None means free text, routed by its wording."""
        now = container.clock.now()
        ask_ai_for: int | None = None
        question = update.effective_message.text or ""
        with session_scope(container.session_factory) as s:
            owner = known_owner(s, update, now)
            if owner is None:
                text = NOT_INVITED
            else:
                if route is None:
                    route = answers.route_text(question)
                    if route == "help" and container.settings.llm_enabled:
                        ask_ai_for = owner.id
                text = answers.answer_for(route, s, owner, now)
        if ask_ai_for is not None:
            text = await ai_answers.answer(
                container.http,
                container.settings,
                container.session_factory,
                ask_ai_for,
                question,
                now,
            )
        await reply(update, text)

    async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _answer(update, None)

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with session_scope(container.session_factory) as s:
            known_owner(s, update, container.clock.now())
        await reply(update, HELP)

    app.add_handler(CommandHandler("start", _safe(start)))
    app.add_handler(CommandHandler("connect", _safe(connect)))
    for route in ("today", "yesterday", "month", "alerts"):
        app.add_handler(CommandHandler(route, _safe(answer_command(route))))
    app.add_handler(CommandHandler("help", _safe(help_cmd)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _safe(text_message)))
    return app

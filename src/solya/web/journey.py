"""Customer web journey: the invite-gated landing page, /join/<invite> and sign-up.

Pages are server-rendered Jinja templates (autoescaped) with no JavaScript. Every form
carries a CSRF token: before sign-up it is bound to the random pre-session cookie
`solya_pre`, afterwards to the signed session cookie."""
from __future__ import annotations

import secrets
from pathlib import Path

import jinja2
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from solya.db import session_scope
from solya.models import Invite
from solya.pricing import VAT_NOTE
from solya.services import onboarding
from solya.web.security import (
    SESSION_COOKIE, SESSION_MAX_AGE, RateLimiter, client_ip, cookie_params,
)

router = APIRouter()

_HERE = Path(__file__).resolve().parent
STATIC_DIR = _HERE / "static"
templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(_HERE / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
)

PRE_COOKIE = "solya_pre"
PRE_MAX_AGE = 24 * 3600
SIGNUP_LIMIT, SIGNUP_WINDOW_S = 10, 3600
AFTER_SIGNUP = "/connect-site"

BAD_INVITE = "הקוד אינו בתוקף"
BAD_FIELDS = "נא למלא שם, אימייל וטלפון תקינים"
ALREADY_LINKED = "כבר נרשמת, המשך בטלגרם"
FORM_EXPIRED = "תוקף הטופס פג. נא לנסות שוב."
TOO_MANY = "יותר מדי ניסיונות. נא לנסות שוב בעוד שעה."


def _container(request: Request):
    return request.app.state.container


def _signup_limiter(request: Request) -> RateLimiter:
    """One in-memory limiter per app, created on first use."""
    state = request.app.state
    limiter = getattr(state, "signup_limiter", None)
    if limiter is None:
        limiter = state.signup_limiter = RateLimiter(
            SIGNUP_LIMIT, SIGNUP_WINDOW_S, _container(request).clock
        )
    return limiter


def _landing(request: Request, *, code: str = "", name: str = "", error: str | None = None,
             status: int = 200):
    """The landing page with a CSRF token bound to the pre-session cookie (set if absent).
    Email and phone are never echoed back: contact details appear only on /admin."""
    c = _container(request)
    pre = request.cookies.get(PRE_COOKIE) or secrets.token_urlsafe(24)
    resp = templates.TemplateResponse(
        request,
        "landing.html",
        {
            "ray": bool(c.settings.ray_implementation),
            "vat_note": VAT_NOTE,
            "csrf": c.sessions.csrf_token(pre),
            "code": code,
            "name": name,
            "error": error,
        },
        status_code=status,
    )
    if request.cookies.get(PRE_COOKIE) != pre:
        resp.set_cookie(PRE_COOKIE, pre, **cookie_params(c.settings.public_base_url, PRE_MAX_AGE))
    return resp


def _message(request: Request, text: str, status: int = 200, *, bot_link: bool = False):
    c = _container(request)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "message": text,
            "bot_url": f"https://t.me/{c.settings.telegram_bot_username}" if bot_link else None,
        },
        status_code=status,
    )


def _signed_in(request: Request, owner_id: int) -> RedirectResponse:
    c = _container(request)
    resp = RedirectResponse(AFTER_SIGNUP, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, c.sessions.issue(owner_id),
        **cookie_params(c.settings.public_base_url, SESSION_MAX_AGE),
    )
    return resp


@router.get("/")
async def landing(request: Request):
    return _landing(request)


def _join_state(request: Request, invite: str) -> tuple[str, int | None, str]:
    """("unknown" | "unused" | "not_resumable" | "linked" | "resumable", owner_id, name)."""
    c = _container(request)
    with session_scope(c.session_factory) as s:
        inv = s.get(Invite, invite)
        if inv is None:
            return "unknown", None, ""
        if inv.used_at is None:
            return "unused", None, ""
        owner = onboarding.web_owner_for_invite(s, invite)
        if owner is None:  # redeemed in the bot, or deactivated
            return "not_resumable", None, ""
        if owner.telegram_chat_id is not None:
            return "linked", owner.id, owner.display_name
        return "resumable", owner.id, owner.display_name


def _join_other(request: Request, invite: str, state: str):
    if state == "unused":
        return _landing(request, code=invite)
    if state == "linked":
        return _message(request, ALREADY_LINKED, bot_link=True)
    return _landing(request, error=BAD_INVITE)


@router.get("/join/{invite}")
async def join(invite: str, request: Request):
    """Pre-fill the code. An invite already redeemed by a web owner who has not linked
    Telegram shows a confirmation; the session is issued only by the CSRF-checked POST
    (a GET never signs anyone in, so a shared link cannot log a victim in as someone
    else). After linking it only says to continue in Telegram (no deep link)."""
    state, _, name = _join_state(request, invite)
    if state != "resumable":
        return _join_other(request, invite, state)
    c = _container(request)
    pre = request.cookies.get(PRE_COOKIE) or secrets.token_urlsafe(24)
    resp = templates.TemplateResponse(
        request, "join_confirm.html",
        {"name": name, "invite": invite, "csrf": c.sessions.csrf_token(pre)},
    )
    if request.cookies.get(PRE_COOKIE) != pre:
        resp.set_cookie(PRE_COOKIE, pre, **cookie_params(c.settings.public_base_url, PRE_MAX_AGE))
    return resp


@router.post("/join/{invite}")
async def join_confirm(invite: str, request: Request, csrf: str = Form("")):
    c = _container(request)
    if not _signup_limiter(request).hit(client_ip(request)):
        return _message(request, TOO_MANY, 429)
    if not c.sessions.check_csrf(request.cookies.get(PRE_COOKIE), csrf):
        return _message(request, FORM_EXPIRED, 403)
    state, owner_id, _ = _join_state(request, invite)
    if state != "resumable":
        return _join_other(request, invite, state)
    return _signed_in(request, owner_id)


@router.post("/signup")
async def signup(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    code: str = Form(""),
    csrf: str = Form(""),
):
    c = _container(request)
    if not _signup_limiter(request).hit(client_ip(request)):
        return _message(request, TOO_MANY, 429)
    if not c.sessions.check_csrf(request.cookies.get(PRE_COOKIE), csrf):
        return _landing(request, code=code.strip(), name=name.strip(), error=FORM_EXPIRED,
                        status=403)
    try:
        with session_scope(c.session_factory) as s:
            owner = onboarding.signup_web(s, code, name, email, phone, c.clock.now())
            owner_id = owner.id if owner is not None else None
    except ValueError as e:
        if str(e) != "form":
            raise
        return _landing(request, code=code.strip(), name=name.strip(), error=BAD_FIELDS,
                        status=400)
    if owner_id is None:
        return _landing(request, code=code.strip(), name=name.strip(), error=BAD_INVITE,
                        status=400)
    return _signed_in(request, owner_id)

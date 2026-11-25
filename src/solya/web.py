"""OAuth connect/callback routes. Never logs query strings, codes or tokens."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from solya.db import session_scope
from solya.services import onboarding
from solya.solaredge.oauth import build_authorize_url, exchange_code

log = logging.getLogger(__name__)
router = APIRouter()

COOKIE = "solya_connect"
INVALID_LINK = "הקישור אינו בתוקף. שלחו /connect בבוט כדי לקבל קישור חדש."
CANCELLED = "החיבור בוטל או נכשל. אפשר לנסות שוב עם /connect."
CONNECTED = "המערכת חוברה בהצלחה. אפשר לחזור לטלגרם."
FAILED = "משהו השתבש בחיבור המערכת. אפשר לנסות שוב עם /connect."


def _html(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f'<html dir="rtl" lang="he"><head><meta charset="utf-8"></head><body><p>{text}</p></body></html>',
        status_code=status,
    )


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.get("/connect/{token}")
async def connect(token: str, request: Request):
    c = request.app.state.container
    ttl = c.settings.connect_token_ttl_minutes
    with session_scope(c.session_factory) as s:
        req = onboarding.resolve_connect_token(s, token, c.clock.now(), ttl)
        nonce = req.nonce if req is not None else None
    if nonce is None:
        return _html(INVALID_LINK, 404)
    resp = RedirectResponse(build_authorize_url(c.settings, nonce), status_code=302)
    resp.set_cookie(COOKIE, token, max_age=ttl * 60, httponly=True, samesite="lax")
    return resp


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str | None = None,
    site_id: int | None = None,
    external_id: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    c = request.app.state.container
    if error:
        return _html(CANCELLED, 400)
    if not code or site_id is None:
        return _html(CANCELLED, 400)
    ttl = c.settings.connect_token_ttl_minutes
    with session_scope(c.session_factory) as s:
        req = onboarding.resolve_callback(
            s, external_id, state, request.cookies.get(COOKIE), c.clock.now(), ttl
        )
        token = req.token if req is not None else None
    if token is None:
        return _html(INVALID_LINK, 400)
    try:
        pair = await exchange_code(c.http, c.settings, code)
        c.tokens.store_new_grant(site_id, pair)
        info = await c.client.get_site(site_id)
        with session_scope(c.session_factory) as s:
            req = s.get(onboarding.ConnectRequest, token)
            site, is_first = onboarding.register_site(s, req, info, c.clock.now())
            sid = site.se_site_id
        await c.reporter.on_site_connected(sid, is_first)
    except Exception as e:
        log.error("oauth callback failed after code exchange: %s", type(e).__name__)
        return _html(FAILED, 502)
    return _html(CONNECTED)

"""OAuth connect/callback routes. Logs query parameter names, never their values,
codes or tokens."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from solya.db import session_scope
from solya.models import Owner, Site
from solya.services import onboarding
from solya.solaredge.oauth import build_authorize_url, exchange_code
from solya.web import journey

log = logging.getLogger(__name__)
router = APIRouter()

COOKIE = "solya_connect"
INVALID_LINK = "הקישור אינו בתוקף. שלחו /connect בבוט כדי לקבל קישור חדש."
CANCELLED = "החיבור בוטל או נכשל. אפשר לנסות שוב עם /connect."
CONNECTED = "המערכת חוברה בהצלחה. אפשר לחזור לטלגרם."
SITE_PICKER = "/sites"
SETTINGS_PAGE = "/settings"
FAILED = "משהו השתבש בחיבור המערכת. אפשר לנסות שוב עם /connect."
# F1: a web sign-up with no Telegram chat cannot use /connect in the bot; it retries on
# the web instead, with a button back to the connect page.
WEB_RETRY = "אפשר לנסות שוב:"
WEB_INVALID_LINK = f"הקישור אינו בתוקף. {WEB_RETRY}"
WEB_CANCELLED = f"החיבור בוטל או נכשל. {WEB_RETRY}"
WEB_FAILED = f"משהו השתבש בחיבור המערכת. {WEB_RETRY}"
_WEB_COPY = {INVALID_LINK: WEB_INVALID_LINK, CANCELLED: WEB_CANCELLED, FAILED: WEB_FAILED}


def _continue_on_web(owner: Owner) -> bool:
    """A web sign-up who has not linked Telegram continues on the site picker; the first
    connected site moves a lead to "connected". Bot owners keep the success page."""
    if owner.signup_source != "web" or owner.telegram_chat_id is not None:
        return False
    if owner.subscription_status == "lead":
        owner.subscription_status = "connected"
    return True


def _html(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f'<html dir="rtl" lang="he"><head><meta charset="utf-8"></head><body><p>{text}</p></body></html>',
        status_code=status,
    )


def _is_unlinked_web_owner(request: Request, owner_id: int | None) -> bool:
    """The owner of the resolved connect request, else the browser's web session."""
    if owner_id is None:
        sess = journey._session(request)
        owner_id = sess[0] if sess is not None else None
    if owner_id is None:
        return False
    with session_scope(request.app.state.container.session_factory) as s:
        owner = s.get(Owner, owner_id)
        return (owner is not None and owner.signup_source == "web"
                and owner.telegram_chat_id is None)


def _failure(request: Request, text: str, status: int, owner_id: int | None = None):
    """A failure page: bot owners keep the /connect copy, an unlinked web owner gets the
    journey template with a button back to /connect-site."""
    if _is_unlinked_web_owner(request, owner_id):
        return journey._message(request, _WEB_COPY[text], status, retry_url=journey.AFTER_SIGNUP)
    return _html(text, status)


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
        return _failure(request, INVALID_LINK, 404)
    resp = RedirectResponse(build_authorize_url(c.settings, nonce), status_code=302)
    resp.set_cookie(COOKIE, token, max_age=ttl * 60, httponly=True, samesite="lax")
    return resp


def _callback_failed(request: Request, reason: str, error: str | None = None,
                     error_description: str | None = None, owner_id: int | None = None):
    """Log why a callback was refused: the parameter names and SolarEdge's error text,
    which are not secret. Never the values (``code`` is a credential)."""
    log.warning(
        "oauth callback refused (%s); params=%s error=%s error_description=%s",
        reason, sorted(request.query_params.keys()), error,
        (error_description or "")[:200] or None,
    )
    return _failure(request, CANCELLED, 400, owner_id)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str | None = None,
    site_id: str | None = None,
    siteId: str | None = None,  # alias: the echoed parameter name is UNVERIFIED
    external_id: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    c = request.app.state.container
    if error:
        return _callback_failed(request, "error from SolarEdge", error, error_description)
    raw_site_id = site_id if site_id is not None else siteId
    if not code or not raw_site_id:
        return _callback_failed(request, "code or site_id missing")
    try:
        site_id = int(raw_site_id)
    except ValueError:
        return _callback_failed(request, "site_id is not a number")
    ttl = c.settings.connect_token_ttl_minutes
    with session_scope(c.session_factory) as s:
        req = onboarding.resolve_callback(
            s, external_id, state, request.cookies.get(COOKIE), c.clock.now(), ttl
        )
        token = req.token if req is not None else None
        owner_id = req.owner_id if req is not None else None
    if token is None:
        log.warning("oauth callback refused (no usable connect request); params=%s cookie=%s",
                    sorted(request.query_params.keys()), COOKIE in request.cookies)
        return _failure(request, INVALID_LINK, 400)
    try:
        pair = await exchange_code(c.http, c.settings, code)
        # M4: prove the new grant can read site_id before anything is stored. SolarEdge's
        # answer is the authority; the site_id query parameter is user-editable.
        info = await c.client.verify_site_access(site_id, pair.access_token)
    except Exception as e:
        log.error("oauth callback refused: code exchange or site access check failed: %s",
                  type(e).__name__)
        return _failure(request, FAILED, 502, owner_id)
    if info.site_id != site_id:
        return _callback_failed(request, "granted site does not match site_id",
                                owner_id=owner_id)
    try:
        # A site owned by someone else moves only because SolarEdge just proved this
        # grant can read it; the admin is told below (M4).
        with session_scope(c.session_factory) as s:
            existing = s.get(Site, site_id)
            previous_owner = existing.owner_id if existing is not None else None
        c.tokens.store_new_grant(site_id, pair)
        with session_scope(c.session_factory) as s:
            req = s.get(onboarding.ConnectRequest, token)
            site, is_first = onboarding.register_site(s, req, info, c.clock.now())
            sid, new_owner = site.se_site_id, site.owner_id
            owner = s.get(Owner, new_owner)
            to_picker = _continue_on_web(owner)
            linked = owner.telegram_chat_id is not None
        already = previous_owner == new_owner
        await c.reporter.on_site_connected(sid, is_first, already=already)
        if previous_owner is not None and previous_owner != new_owner:
            await c.reporter.on_site_reassigned(sid, previous_owner, new_owner)
    except Exception as e:
        log.error("oauth callback failed after the site access check: %s", type(e).__name__)
        return _failure(request, FAILED, 502, owner_id)
    banner = f"{'already' if already else 'connected'}={sid}"
    if to_picker:
        return RedirectResponse(f"{SITE_PICKER}?{banner}", status_code=303)
    # a linked owner who started from their settings page (a live session of the same
    # owner) goes back there; the Telegram /connect link has no session and gets the page
    if linked and c.sessions.read(request.cookies.get(journey.SESSION_COOKIE)) == new_owner:
        return RedirectResponse(f"{SETTINGS_PAGE}?{banner}", status_code=303)
    return _html(CONNECTED)

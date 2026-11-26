"""Read-only admin page behind a password login. Without ADMIN_PASSWORD every /admin*
route is a 404. The page renders contact details, status and usage, never token material
(it does not even read the token table)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from solya.db import session_scope
from solya.models import LlmUsage, Owner, RawResponse, RayDaily, Site
from solya.services import ai_answers
from solya.web.journey import (
    FORM_EXPIRED, PRE_COOKIE, PRE_MAX_AGE, _container, _message, templates,
)
from solya.web.security import (
    ADMIN_COOKIE, ADMIN_MAX_AGE, RateLimiter, client_ip, cookie_params,
)

router = APIRouter(prefix="/admin")

LOGIN_LIMIT, LOGIN_WINDOW_S = 5, 15 * 60
BAD_PASSWORD = "הסיסמה שגויה"
LOCKED_OUT = "יותר מדי ניסיונות. נא לנסות שוב בעוד רבע שעה."
SUBSCRIPTION_LABELS = {
    "lead": "נרשם", "connected": "מחובר", "pending_payment": "ממתין לתשלום", "active": "פעיל",
}
SOURCE_LABELS = {"web": "אתר", "bot": "בוט"}
LOW_PLAN_NOTE = "תוכנית חסכונית: ללא זיהוי תקלות ממיר"  # spec 5.5
SITE_STATUS_LABELS = {"active": "פעיל", "reauth_needed": "דורש חיבור מחדש", "paused": "מושהה"}
DISPLAY_TZ = ZoneInfo("Asia/Jerusalem")
CONFIDENCE_LABELS = {"normal": "רגילה", "low": "נמוכה"}


def _password(request: Request) -> bytes:
    """The configured password; 404 for every /admin route when none is set."""
    secret = _container(request).settings.admin_password
    if secret is None or not secret.get_secret_value():
        raise HTTPException(404)
    return secret.get_secret_value().encode()


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _limiter(request: Request) -> RateLimiter:
    state = request.app.state
    limiter = getattr(state, "admin_limiter", None)
    if limiter is None:
        limiter = state.admin_limiter = RateLimiter(
            LOGIN_LIMIT, LOGIN_WINDOW_S, _container(request).clock
        )
    return limiter


def _is_admin(request: Request) -> bool:
    """A valid admin cookie signed after the last logout (kept in memory: a restart
    forgets it, so old cookies then stay valid until they expire)."""
    return _container(request).sessions.read_admin(
        request.cookies.get(ADMIN_COOKIE), getattr(request.app.state, "admin_logout_at", None)
    )


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _login_page(request: Request, *, error: str | None = None, status: int = 200):
    c = _container(request)
    pre = request.cookies.get(PRE_COOKIE) or secrets.token_urlsafe(24)
    resp = templates.TemplateResponse(
        request, "admin_login.html", {"csrf": c.sessions.csrf_token(pre), "error": error},
        status_code=status,
    )
    if request.cookies.get(PRE_COOKIE) != pre:
        resp.set_cookie(PRE_COOKIE, pre, **cookie_params(c.settings.public_base_url, PRE_MAX_AGE))
    return _no_store(resp)


@router.get("/login")
async def login_page(request: Request):
    _password(request)
    if _is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return _login_page(request)


@router.post("/login")
async def login(request: Request, password: str = Form(""), csrf: str = Form("")):
    expected = _password(request)
    c = _container(request)
    limiter, ip = _limiter(request), client_ip(request)
    if limiter.blocked(ip):
        return _message(request, LOCKED_OUT, 429)
    if not c.sessions.check_csrf(request.cookies.get(PRE_COOKIE), csrf):
        return _login_page(request, error=FORM_EXPIRED, status=403)
    if not hmac.compare_digest(_digest(password.encode()), _digest(expected)):
        limiter.hit(ip)  # only failures count towards the lockout
        return _login_page(request, error=BAD_PASSWORD, status=401)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        ADMIN_COOKIE, c.sessions.issue_admin(),
        **cookie_params(c.settings.public_base_url, ADMIN_MAX_AGE),
    )
    return resp


@router.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    _password(request)
    c = _container(request)
    cookie = request.cookies.get(ADMIN_COOKIE)
    if not _is_admin(request):  # expired or already revoked: nothing to log out of
        return RedirectResponse("/admin/login", status_code=303)
    if not c.sessions.check_csrf(cookie, csrf):
        return _message(request, FORM_EXPIRED, 403)
    request.app.state.admin_logout_at = c.clock.now()  # revoke every cookie issued so far
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


def _fmt(ts: datetime | None) -> str | None:
    return ts.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M") if ts else None


@router.get("")
async def admin_page(request: Request):
    _password(request)
    if not _is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    c = _container(request)
    now = c.clock.now()
    cycle_start = c.budget.cycle_start()
    month_start = ai_answers.month_start(now)  # the AI budget's month (M8)
    rows = []
    with session_scope(c.session_factory) as s:
        last_pull = dict(s.execute(
            select(RawResponse.site_id, func.max(RawResponse.fetched_at))
            .group_by(RawResponse.site_id)
        ).all())
        tokens = dict(s.execute(
            select(LlmUsage.owner_id, func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens))
            .where(LlmUsage.ts >= month_start).group_by(LlmUsage.owner_id)
        ).all())
        yesterday = (now.astimezone(DISPLAY_TZ) - timedelta(days=1)).date()
        ray_rows = {
            r.site_id: r for r in s.scalars(
                select(RayDaily).where(RayDaily.local_date == yesterday))
        }
        sites_by_owner: dict[int, list[Site]] = {}
        for site in s.scalars(select(Site).order_by(Site.created_at, Site.se_site_id)):
            sites_by_owner.setdefault(site.owner_id, []).append(site)
        for owner in s.scalars(select(Owner).order_by(Owner.id)):
            sites = sites_by_owner.get(owner.id, [])
            pulls = [last_pull[x.se_site_id] for x in sites if x.se_site_id in last_pull]
            rows.append({
                "name": owner.display_name,
                "email": owner.email,
                "phone": owner.phone,
                "source": SOURCE_LABELS.get(owner.signup_source, owner.signup_source),
                "status": SUBSCRIPTION_LABELS.get(
                    owner.subscription_status, owner.subscription_status),
                "linked": owner.telegram_chat_id is not None,
                "sites": [{
                    "name": x.card_name or x.portal_name,
                    "id": x.se_site_id,
                    "monitored": x.monitored,
                    "call_plan": x.call_plan,
                    "plan_note": LOW_PLAN_NOTE if x.call_plan == "low" else None,
                    "status": SITE_STATUS_LABELS.get(x.status, x.status),
                    "comm_state": x.comm_state,
                    "ray": ray_rows[x.se_site_id].ray if x.se_site_id in ray_rows else None,
                    "ray_confidence": CONFIDENCE_LABELS.get(
                        ray_rows[x.se_site_id].confidence, ray_rows[x.se_site_id].confidence
                    ) if x.se_site_id in ray_rows else None,
                } for x in sites],
                "last_pull": _fmt(max(pulls)) if pulls else None,
                "credits": sum(c.budget.used_by_site(x.se_site_id, cycle_start) for x in sites),
                "ai_tokens": int(tokens.get(owner.id) or 0),
            })
    return _no_store(templates.TemplateResponse(
        request, "admin.html",
        {
            "rows": rows,
            "used": c.budget.used_this_cycle(),
            "cap": c.settings.budget_monthly_credits,
            "csrf": c.sessions.csrf_token(request.cookies.get(ADMIN_COOKIE)),
        },
    ))

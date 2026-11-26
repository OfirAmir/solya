"""Customer web journey: the invite-gated landing page, /join/<invite> and sign-up, then
connect, site picker, price, payment stub and the Telegram hand-off.

Pages are server-rendered Jinja templates (autoescaped) with no JavaScript. Every form
carries a CSRF token: before sign-up it is bound to the random pre-session cookie
`solya_pre`, afterwards to the signed session cookie. Session pages without a valid
session (or whose owner is no longer active) redirect with 303 to /."""
from __future__ import annotations

import secrets
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import jinja2
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from fastapi.templating import Jinja2Templates

from solya.db import session_scope
from solya.models import Invite, Owner, Payment, Site
from solya.pricing import (
    ESTIMATE_LABEL, UNKNOWN_PRICE, VAT_NOTE, describe_price, price_site, price_total,
)
from solya.services import onboarding, payments, site_settings
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
PICK_ONE = "יש לבחור לפחות מערכת אחת"
CHANGES_VIA_BOT = "לשינויים שלחו לסול בטלגרם /settings"
NOT_FOUND = "העמוד לא נמצא."
PAYMENT_DESCRIPTION = "מנוי חודשי, ספק טרם נבחר"


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


def _message(request: Request, text: str, status: int = 200, *, bot_link: bool = False,
             retry_url: str | None = None):
    c = _container(request)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "message": text,
            "bot_url": f"https://t.me/{c.settings.telegram_bot_username}" if bot_link else None,
            "retry_url": retry_url,
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


# --- session pages: connect, site picker, price, payment stub, ready -----------------------

def _to_landing() -> RedirectResponse:
    return RedirectResponse("/", status_code=303)


def _session(request: Request) -> tuple[int, str] | None:
    """(owner_id, CSRF token) for a valid session cookie whose owner is active."""
    c = _container(request)
    cookie = request.cookies.get(SESSION_COOKIE)
    owner_id = c.sessions.read(cookie)
    if owner_id is None:
        return None
    with session_scope(c.session_factory) as s:
        owner = s.get(Owner, owner_id)
        if owner is None or not owner.active:
            return None
    return owner_id, c.sessions.csrf_token(cookie)


def _csrf_ok(request: Request, token: str) -> bool:
    return _container(request).sessions.check_csrf(request.cookies.get(SESSION_COOKIE), token)


def _owner_sites(s, owner_id: int) -> list[Site]:
    """Only ever this owner's sites (Review Focus 2)."""
    return list(s.scalars(
        select(Site).where(Site.owner_id == owner_id).order_by(Site.created_at, Site.se_site_id)
    ))


def _ils(agorot: int) -> str:
    return f"{agorot / 100:.2f} ₪"


@router.get("/connect-site")
async def connect_site(request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    owner_id, csrf = sess
    c = _container(request)
    with session_scope(c.session_factory) as s:
        owner = s.get(Owner, owner_id)
        first_name = (owner.display_name.split() or [""])[0]
        connected = [site_settings.display_name(site) for site in _owner_sites(s, owner_id)]
        # a linked owner's /sites is locked: they choose systems on /settings
        back = "/settings" if owner.telegram_chat_id is not None else "/sites"
    return templates.TemplateResponse(
        request, "connect.html",
        {"first_name": first_name, "connected": connected, "csrf": csrf, "back": back},
    )


@router.post("/connect-site")
async def connect_site_start(request: Request, csrf: str = Form("")):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    if not _csrf_ok(request, csrf):
        return _message(request, FORM_EXPIRED, 403)
    c = _container(request)
    with session_scope(c.session_factory) as s:
        url = onboarding.create_connect_request(s, sess[0], c.clock.now(), c.settings)
    # same host as this page, so the session cookie is there when the callback lands
    return RedirectResponse(urlsplit(url).path, status_code=303)


def banner(request: Request, s, owner_id: int) -> dict | None:
    """The after-connect banner from ``?connected=<id>`` or ``?already=<id>``, only for a
    site of this owner (anything else shows no banner). Shared with /settings."""
    for kind in ("connected", "already"):
        raw = request.query_params.get(kind)
        if raw is None:
            continue
        if not (raw.isascii() and raw.isdigit() and len(raw) <= 18):  # fits a SQLite integer
            return None
        site = s.get(Site, int(raw))
        if site is None or site.owner_id != owner_id:
            return None
        return {"kind": kind, "name": site_settings.display_name(site)}
    return None


def tariff_text(tariff: Decimal | None) -> str:
    return "" if tariff is None else f"{tariff.normalize():f}"


def site_field_rows(sites: list[Site], form=None,
                    errors: dict[int, dict[str, str]] | None = None) -> list[dict]:
    """Rows for ``_site_fields.html``: the stored values, or the submitted ones when a
    form is re-rendered (``form``), with each site's field errors. Shared with /settings."""
    rows = []
    chosen = site_settings.monitored_choices(form, sites) if form is not None else {}
    for site in sites:
        sid = site.se_site_id
        stored = {"name": site_settings.display_name(site), "basis": site.income_basis,
                  "tariff": tariff_text(site.tariff_ils_per_kwh)}
        if form is not None:
            for field in stored:
                value = form.get(f"{field}_{sid}")
                if value is not None:
                    stored[field] = str(value)
        p = price_site(site)
        rows.append({
            "id": sid,
            "title": site_settings.display_name(site),
            **stored,
            "errors": (errors or {}).get(sid, {}),
            "monitored": chosen.get(sid, site.monitored),
            "portal_kw": site.peak_power_kwp_reported,
            "price": describe_price(p.agorot, p.is_estimate),
        })
    return rows


def site_today(c):
    """Site-local today, for recomputing this month's income on save."""
    return lambda site: c.clock.now().astimezone(ZoneInfo(site.tz)).date()


def _sites_page(request: Request, owner_id: int, csrf: str, *, error: str | None = None,
                status: int = 200, form=None, errors: dict[int, dict[str, str]] | None = None):
    """``form`` (the submitted values) replaces the stored ones when re-rendering."""
    c = _container(request)
    with session_scope(c.session_factory) as s:
        linked = s.get(Owner, owner_id).telegram_chat_id is not None
        rows = site_field_rows(_owner_sites(s, owner_id), form, errors)
        shown = banner(request, s, owner_id)
    return templates.TemplateResponse(
        request, "sites.html",
        {"sites": rows, "linked": linked, "csrf": csrf, "error": error,
         "via_bot": CHANGES_VIA_BOT, "banner": shown},
        status_code=status,
    )


@router.get("/sites")
async def sites(request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    return _sites_page(request, *sess)


@router.post("/sites")
async def sites_save(request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    owner_id, csrf = sess
    form = await request.form()
    if not _csrf_ok(request, str(form.get("csrf", ""))):
        return _message(request, FORM_EXPIRED, 403)
    c = _container(request)
    with session_scope(c.session_factory) as s:
        if s.get(Owner, owner_id).telegram_chat_id is not None:
            return _message(request, CHANGES_VIA_BOT, 409, bot_link=True)
        # only this owner's sites are read: ids of others in the form are ignored
        owned = _owner_sites(s, owner_id)
        edits, errors = site_settings.parse_site_form(form, owned)
        picked = any(site_settings.monitored_choices(form, owned).values())
        if picked and not errors:
            site_settings.apply_edits(s, owner_id, edits, site_today(c))
    if errors:
        return _sites_page(request, owner_id, csrf, status=400, form=form, errors=errors)
    if not picked:
        return _sites_page(request, owner_id, csrf, error=PICK_ONE, status=400, form=form)
    return RedirectResponse("/price", status_code=303)


def _monitored_total(s, owner_id: int) -> tuple[list[Site], int | None, bool]:
    monitored = [site for site in _owner_sites(s, owner_id) if site.monitored]
    agorot, estimate = price_total([price_site(site) for site in monitored])
    return monitored, agorot, estimate


@router.get("/price")
async def price(request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    owner_id, csrf = sess
    c = _container(request)
    with session_scope(c.session_factory) as s:
        monitored, agorot, estimate = _monitored_total(s, owner_id)
        rows = [{"name": site_settings.display_name(site),
                 "price": describe_price(p.agorot, p.is_estimate)}
                for site, p in ((site, price_site(site)) for site in monitored)]
    if not monitored:
        return RedirectResponse("/sites", status_code=303)
    return templates.TemplateResponse(
        request, "price.html",
        {"rows": rows, "total": describe_price(agorot, estimate), "csrf": csrf},
    )


@router.post("/pay")
async def pay(request: Request, csrf: str = Form("")):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    if not _csrf_ok(request, csrf):
        return _message(request, FORM_EXPIRED, 403)
    owner_id = sess[0]
    c = _container(request)
    with session_scope(c.session_factory) as s:
        monitored, agorot, estimate = _monitored_total(s, owner_id)
        if not monitored:
            return RedirectResponse("/sites", status_code=303)
        latest = s.scalars(
            select(Payment)
            .where(Payment.owner_id == owner_id, Payment.status == "stub_pending")
            .order_by(Payment.created_at.desc())
            .limit(1)
        ).first()
        if (latest is not None and latest.amount_agorot == agorot
                and latest.amount_is_estimate == estimate):
            payment_id = latest.id  # a repeated POST reuses the same pending stub
        else:
            payment_id = payments.create_stub_checkout(
                s, owner_id, agorot, estimate, PAYMENT_DESCRIPTION, c.clock.now()
            ).id
    return RedirectResponse(f"/pay/{payment_id}", status_code=303)


@router.get("/pay/{payment_id}")
async def pay_page(payment_id: str, request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    c = _container(request)
    with session_scope(c.session_factory) as s:
        payment = s.get(Payment, payment_id)
        if payment is None or payment.owner_id != sess[0]:  # Review Focus 2
            return _message(request, NOT_FOUND, 404)
        agorot, estimate = payment.amount_agorot, payment.amount_is_estimate
    return templates.TemplateResponse(
        request, "pay.html",
        {
            "amount": _ils(agorot) if agorot is not None else None,
            "estimate_label": ESTIMATE_LABEL if estimate else None,
            "unknown_price": UNKNOWN_PRICE,
            "vat_note": VAT_NOTE,
        },
    )


@router.get("/ready")
async def ready(request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    c = _container(request)
    with session_scope(c.session_factory) as s:
        owner = s.get(Owner, sess[0])
        linked, link_token = owner.telegram_chat_id is not None, owner.link_token
    if linked or not link_token:  # the token is cleared only when the chat is linked
        return _message(request, ALREADY_LINKED, bot_link=True)
    bot = c.settings.telegram_bot_username
    return templates.TemplateResponse(
        request, "ready.html", {"deep_link": f"https://t.me/{bot}?start={link_token}"},
    )

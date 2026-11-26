"""Owner settings: the single-use link the bot sends for /settings, and the settings page.

``GET /settings/<token>`` only shows a confirmation (Telegram's link preview must not use
up the token, and a GET never signs anyone in). The CSRF-checked POST claims the token
and starts a 24-hour session. A used, expired or unknown token gets the "expired" page
with status 410, and never a session."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from solya.db import session_scope
from solya.models import Owner
from solya.pricing import describe_price, price_site, price_total
from solya.services import onboarding, site_settings
from solya.web.journey import (
    FORM_EXPIRED, PICK_ONE, PRE_COOKIE, PRE_MAX_AGE, TOO_MANY, _container, _csrf_ok,
    _message, _owner_sites, _session, _signup_limiter, _to_landing, banner, site_field_rows,
    site_today, templates,
)
from solya.web.security import (
    SESSION_COOKIE, SETTINGS_SESSION_MAX_AGE, client_ip, cookie_params,
)

router = APIRouter()

LINK_EXPIRED = "הקישור פג. שלחו /settings לסול לקישור חדש."
SAVED = "נשמר."


def _link_owner(request: Request, token: str) -> tuple[int, str] | None:
    """(owner id, name) for a usable link of an active owner."""
    c = _container(request)
    with session_scope(c.session_factory) as s:
        link = onboarding.resolve_settings_link(s, token, c.clock.now())
        if link is None:
            return None
        owner = s.get(Owner, link.owner_id)
        if owner is None or not owner.active:
            return None
        return owner.id, owner.display_name


def _expired(request: Request):
    return _message(request, LINK_EXPIRED, 410, bot_link=True)


@router.get("/settings/{token}")
async def settings_link(token: str, request: Request):
    found = _link_owner(request, token)
    if found is None:
        return _expired(request)
    c = _container(request)
    pre = request.cookies.get(PRE_COOKIE) or secrets.token_urlsafe(24)
    resp = templates.TemplateResponse(
        request, "settings_confirm.html",
        {"name": found[1], "token": token, "csrf": c.sessions.csrf_token(pre)},
    )
    if request.cookies.get(PRE_COOKIE) != pre:
        resp.set_cookie(PRE_COOKIE, pre, **cookie_params(c.settings.public_base_url, PRE_MAX_AGE))
    return resp


@router.post("/settings/{token}")
async def settings_link_open(token: str, request: Request, csrf: str = Form("")):
    c = _container(request)
    if not _signup_limiter(request).hit(client_ip(request)):
        return _message(request, TOO_MANY, 429)
    if not c.sessions.check_csrf(request.cookies.get(PRE_COOKIE), csrf):
        return _message(request, FORM_EXPIRED, 403)
    if _link_owner(request, token) is None:  # also refuses a deactivated owner
        return _expired(request)
    with session_scope(c.session_factory) as s:
        owner_id = onboarding.use_settings_link(s, token, c.clock.now())
    if owner_id is None:  # lost a race for the same link
        return _expired(request)
    resp = RedirectResponse("/settings", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, c.sessions.issue(owner_id, max_age=SETTINGS_SESSION_MAX_AGE),
        **cookie_params(c.settings.public_base_url, SETTINGS_SESSION_MAX_AGE),
    )
    return resp


def _settings_page(request: Request, owner_id: int, csrf: str, *, saved: str | None = None,
                   error: str | None = None, status: int = 200, form=None,
                   errors: dict[int, dict[str, str]] | None = None):
    c = _container(request)
    with session_scope(c.session_factory) as s:
        sites = _owner_sites(s, owner_id)
        rows = site_field_rows(sites, form, errors)
        agorot, estimate = price_total([price_site(site) for site in sites if site.monitored])
        shown = banner(request, s, owner_id)
    return templates.TemplateResponse(
        request, "settings.html",
        {"sites": rows, "total": describe_price(agorot, estimate), "csrf": csrf,
         "saved": saved, "error": error, "banner": shown},
        status_code=status,
    )


@router.get("/settings")
async def settings_page(request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    return _settings_page(request, *sess)


@router.post("/settings")
async def settings_save(request: Request):
    sess = _session(request)
    if sess is None:
        return _to_landing()
    owner_id, csrf = sess
    form = await request.form()
    if not _csrf_ok(request, str(form.get("csrf", ""))):
        return _message(request, FORM_EXPIRED, 403)
    c = _container(request)
    notice = None
    with session_scope(c.session_factory) as s:
        # only this owner's sites are read: ids of others in the form are ignored
        sites = _owner_sites(s, owner_id)
        edits, errors = site_settings.parse_site_form(form, sites)
        picked = any(site_settings.monitored_choices(form, sites).values())
        if picked and not errors:
            changed = site_settings.apply_edits(s, owner_id, edits, site_today(c))
            if changed:
                by_id = {site.se_site_id: site for site in sites}
                names = ", ".join(site_settings.display_name(by_id[sid]) for sid in changed)
                agorot, estimate = price_total(
                    [price_site(site) for site in sites if site.monitored]
                )
                notice = (
                    f"בעלים {owner_id} שינה מעקב: {names}. "
                    f"מחיר חודשי חדש: {describe_price(agorot, estimate)}"
                )
    if errors:
        return _settings_page(request, owner_id, csrf, status=400, form=form, errors=errors)
    if not picked:
        return _settings_page(request, owner_id, csrf, error=PICK_ONE, status=400, form=form)
    if notice is not None:
        stamp = c.clock.now().isoformat()
        await c.reporter.notify_admins(notice, f"admin_monitoring:{owner_id}:{stamp}")
    return _settings_page(request, owner_id, csrf, saved=SAVED)

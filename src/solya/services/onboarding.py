"""Invite-only onboarding and OAuth connect-request bookkeeping."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from solya.config import Settings
from solya.models import ConnectRequest, Invite, Owner, Site
from solya.solaredge.models import SiteInfo

COMMERCIAL_KWP = 50


def create_invite(session: Session, created_by: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(24)
    session.add(Invite(token=token, created_by=created_by, created_at=now))
    session.flush()
    return token


def get_owner_by_chat(session: Session, chat_id: int) -> Owner | None:
    return session.scalar(select(Owner).where(Owner.telegram_chat_id == chat_id))


def redeem_invite(
    session: Session, token: str, chat_id: int, first_name: str, now: datetime | None = None
) -> Owner | None:
    now = now or datetime.now(timezone.utc)
    existing = get_owner_by_chat(session, chat_id)
    if existing is not None:
        return existing
    invite = session.get(Invite, token)
    if invite is None or invite.used_at is not None:
        return None
    owner = Owner(
        display_name=first_name,
        telegram_chat_id=chat_id,
        active=True,
        last_inbound_at=now,
        created_at=now,
    )
    session.add(owner)
    session.flush()
    invite.used_at = now
    invite.owner_id = owner.id
    session.flush()
    return owner


def create_connect_request(
    session: Session, owner_id: int, now: datetime, settings: Settings
) -> str:
    """Create a fresh single-use request and return the connect URL."""
    token = secrets.token_urlsafe(24)
    session.add(
        ConnectRequest(
            token=token, owner_id=owner_id, nonce=secrets.token_urlsafe(24), created_at=now
        )
    )
    session.flush()
    return f"{str(settings.public_base_url).rstrip('/')}/connect/{token}"


def _usable(req: ConnectRequest | None, now: datetime, ttl_minutes: int) -> ConnectRequest | None:
    if req is None or req.used_at is not None:
        return None
    if now - req.created_at > timedelta(minutes=ttl_minutes):
        return None
    return req


def resolve_connect_token(
    session: Session, token: str, now: datetime, ttl_minutes: int
) -> ConnectRequest | None:
    return _usable(session.get(ConnectRequest, token), now, ttl_minutes)


def resolve_callback(
    session: Session,
    external_id: str | None,
    state: str | None,
    cookie_token: str | None,
    now: datetime,
    ttl_minutes: int,
) -> ConnectRequest | None:
    for nonce in (external_id, state):
        if nonce:
            req = _usable(
                session.scalar(select(ConnectRequest).where(ConnectRequest.nonce == nonce)),
                now,
                ttl_minutes,
            )
            if req is not None:
                return req
    if cookie_token:
        return _usable(session.get(ConnectRequest, cookie_token), now, ttl_minutes)
    return None


def register_site(
    session: Session, req: ConnectRequest, info: SiteInfo, now: datetime
) -> tuple[Site, bool]:
    """Create or update the Site for a completed connect. Returns (site, first_for_owner)."""
    site = session.get(Site, info.site_id)
    if site is None:
        first = (
            session.scalar(select(func.count()).select_from(Site).where(Site.owner_id == req.owner_id))
            == 0
        )
        site = Site(
            se_site_id=info.site_id,
            owner_id=req.owner_id,
            portal_name=info.name,
            commercial=(info.peak_power_kwp or 0) >= COMMERCIAL_KWP,
            peak_power_kwp_reported=info.peak_power_kwp,
            tz=info.timezone,
            lat=info.lat,
            lon=info.lon,
            last_update_time=info.last_update_time,
            status="active",
            created_at=now,
        )
        session.add(site)
    else:
        first = False
        site.owner_id = req.owner_id
        site.portal_name = info.name
        site.peak_power_kwp_reported = info.peak_power_kwp
        site.tz = info.timezone
        site.lat = info.lat
        site.lon = info.lon
        site.last_update_time = info.last_update_time
        site.status = "active"
    req.used_at = now
    req.site_id = info.site_id
    session.flush()
    return site, first

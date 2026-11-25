"""Invite-only onboarding and OAuth connect-request bookkeeping."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from solya.config import Settings
from solya.rules.faults import ack_owner_faults
from solya.models import ConnectRequest, Invite, Owner, Site
from solya.solaredge.models import SiteInfo

log = logging.getLogger(__name__)

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
    if not _claim_invite(session, token, now):
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
    _attach_invite(session, token, owner.id)
    return owner


def _claim_invite(session: Session, token: str, now: datetime) -> bool:
    """Mark an unused invite used with one conditional UPDATE, so two concurrent
    redemptions (web or bot) cannot both pass a read-then-write check."""
    claimed = session.execute(
        update(Invite)
        .where(Invite.token == token, Invite.used_at.is_(None))
        .values(used_at=now)
    ).rowcount
    return claimed == 1


def _attach_invite(session: Session, token: str, owner_id: int) -> None:
    session.execute(update(Invite).where(Invite.token == token).values(owner_id=owner_id))


MAX_NAME, MAX_EMAIL, MAX_PHONE = 100, 254, 32


def _valid_signup_fields(name: str, email: str, phone: str) -> bool:
    """PR #1's rules (name of 2+ chars, an "@" in the email, 9+ ASCII digits in the
    phone) plus length caps."""
    return (
        2 <= len(name) <= MAX_NAME
        and "@" in email and len(email) <= MAX_EMAIL
        and len(phone) <= MAX_PHONE
        and sum(ch in "0123456789" for ch in phone) >= 9
    )


def signup_web(
    session: Session, invite_token: str, name: str, email: str, phone: str, now: datetime
) -> Owner | None:
    """Create a web Owner and redeem its invite in the caller's transaction.

    Raises ValueError("form") for bad fields (the invite is not touched). Returns None
    when the invite is unknown or already used: a reused code never creates a second
    Owner. Getting back into an unlinked web sign-up goes through /join
    (`web_owner_for_invite`), not through a second sign-up."""
    name, email, phone = name.strip(), email.strip(), phone.strip()
    if not _valid_signup_fields(name, email, phone):
        raise ValueError("form")
    token = invite_token.strip()
    if not _claim_invite(session, token, now):
        return None
    owner = Owner(
        display_name=name,
        email=email,
        phone=phone,
        telegram_chat_id=None,
        signup_source="web",
        subscription_status="lead",
        link_token=secrets.token_urlsafe(24),
        active=True,
        created_at=now,
    )
    session.add(owner)
    session.flush()
    _attach_invite(session, token, owner.id)
    return owner


def web_owner_for_invite(session: Session, invite_token: str) -> Owner | None:
    """The active web Owner that redeemed this invite, or None (unknown, unused, a bot
    sign-up, or a deactivated owner)."""
    invite = session.get(Invite, invite_token)
    if invite is None or invite.owner_id is None:
        return None
    owner = session.get(Owner, invite.owner_id)
    if owner is None or owner.signup_source != "web" or not owner.active:
        return None
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
        if site.owner_id != req.owner_id:  # M4: the caller also tells the admin
            log.warning("site %s reassigned from owner %s to owner %s",
                        info.site_id, site.owner_id, req.owner_id)
            site.monitored = True  # the new owner has not chosen yet; plan and kW are kept
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


def record_inbound(session: Session, owner: Owner, now: datetime) -> None:
    """Inbound message side effects: stamp last_inbound_at, ack notified faults,
    and reactivate an owner who had blocked the bot (R16)."""
    owner.last_inbound_at = now
    if not owner.active:
        owner.active = True
    ack_owner_faults(session, owner.id, now)
    session.flush()

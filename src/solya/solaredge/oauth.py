import calendar
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import httpx

from solya.clock import Clock
from solya.config import Settings
from solya.crypto import TokenCipher
from solya.db import SessionFactory, session_scope
from solya.models import Site, SiteToken
from solya.solaredge.budget import CreditBudget
from solya.solaredge.errors import ApiError, AuthExpired, RateLimited, TransientAuthError

log = logging.getLogger(__name__)

TOKEN_ENDPOINT = "oauth2/token"
REFRESH_MARGIN = timedelta(seconds=300)
_TERMINAL_ERRORS = ("invalid_grant", "invalid_request")


@dataclass(frozen=True)
class TokenPair:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int

    def __repr__(self) -> str:
        return f"TokenPair(access_token=<redacted>, refresh_token=<redacted>, expires_in={self.expires_in})"


def _redirect_uri(settings: Settings) -> str:
    return str(settings.public_base_url).rstrip("/") + "/oauth/callback"


def build_authorize_url(settings: Settings, nonce: str) -> str:
    params = {
        "client_id": settings.solaredge_client_id,
        "scope": settings.se_scope,
        "redirect_uri": _redirect_uri(settings),
        "state": nonce,
        "external_id": nonce,
        "access_duration": settings.se_access_duration_months,
    }
    return f"{settings.se_authorize_url}?{urlencode(params, quote_via=quote)}"


def _fail(exc: Exception, status: int) -> Exception:
    exc.http_status = status  # type: ignore[attr-defined]
    return exc


async def _post_token(http: httpx.AsyncClient, settings: Settings, payload: dict) -> TokenPair:
    body = {
        **payload,
        "client_id": settings.solaredge_client_id,
        "client_secret": settings.solaredge_client_secret.get_secret_value(),
    }
    url = f"{settings.se_api_base.rstrip('/')}/{TOKEN_ENDPOINT}"
    try:
        resp = await http.post(url, json=body)
    except httpx.TransportError as e:
        raise _fail(TransientAuthError(f"token endpoint unreachable: {type(e).__name__}"), 0) from None
    status = resp.status_code
    if status == 200:
        try:
            data = resp.json()
            return TokenPair(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_in=int(data["expires_in"]),
            )
        except (ValueError, KeyError, TypeError):
            raise _fail(ApiError(status, "malformed token response"), status) from None
    if status == 429:
        try:
            retry_after = float(resp.headers.get("retry-after", ""))
        except ValueError:
            retry_after = None
        raise _fail(RateLimited("token endpoint rate limited", retry_after=retry_after), status)
    if status >= 500:
        raise _fail(TransientAuthError(f"token endpoint returned {status}"), status)
    if status in (400, 401):
        try:
            error = resp.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if error in _TERMINAL_ERRORS:
            raise _fail(AuthExpired(f"token rejected: {error}"), status)
        raise _fail(ApiError(status, f"token request rejected: {error or status}"), status)
    raise _fail(ApiError(status), status)


async def exchange_code(http: httpx.AsyncClient, settings: Settings, code: str) -> TokenPair:
    return await _post_token(http, settings, {"grant_type": "authorization_code", "code": code})


async def refresh_tokens(http: httpx.AsyncClient, settings: Settings, refresh_token: str) -> TokenPair:
    return await _post_token(http, settings, {"grant_type": "refresh_token", "refresh_token": refresh_token})


def _add_months(dt: datetime, months: int) -> datetime:
    idx = dt.month - 1 + months
    year, month = dt.year + idx // 12, idx % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))


class TokenManager:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        cipher: TokenCipher,
        http: httpx.AsyncClient,
        budget: CreditBudget,
        clock: Clock,
        on_reauth_needed: Callable[[int], Awaitable[None]],
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._cipher = cipher
        self._http = http
        self._budget = budget
        self._clock = clock
        self._on_reauth_needed = on_reauth_needed
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, site_id: int) -> asyncio.Lock:
        return self._locks.setdefault(site_id, asyncio.Lock())

    def store_new_grant(self, site_id: int, pair: TokenPair) -> None:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            row = s.get(SiteToken, site_id)
            if row is None:
                row = SiteToken(site_id=site_id)
                s.add(row)
            row.access_enc = self._cipher.encrypt(pair.access_token)
            row.refresh_enc = self._cipher.encrypt(pair.refresh_token)
            row.previous_refresh_enc = None
            row.access_expires_at = now + timedelta(seconds=pair.expires_in)
            row.refreshed_at = now
            row.authorized_at = now
            row.grant_expires_at = _add_months(now, self._settings.se_access_duration_months)

    async def get_access_token(self, site_id: int) -> str:
        async with self._lock(site_id):
            self._raise_if_reauth_needed(site_id)
            with session_scope(self._factory) as s:
                row = s.get(SiteToken, site_id)
                if row is None:
                    raise LookupError(f"no token stored for site {site_id}")
                if row.access_expires_at - self._clock.now() > REFRESH_MARGIN:
                    return self._cipher.decrypt(row.access_enc)
            return await self._refresh_locked(site_id)

    async def force_refresh(self, site_id: int) -> None:
        async with self._lock(site_id):
            self._raise_if_reauth_needed(site_id)
            await self._refresh_locked(site_id)

    def _raise_if_reauth_needed(self, site_id: int) -> None:
        with session_scope(self._factory) as s:
            site = s.get(Site, site_id)
            if site is not None and site.status == "reauth_needed":
                raise AuthExpired(f"site {site_id} needs re-authorization")

    async def _call_refresh(self, site_id: int, refresh_token: str) -> TokenPair:
        async with self._budget.call_lock:
            await self._budget.acquire(site_id, TOKEN_ENDPOINT, "essential")
            try:
                pair = await refresh_tokens(self._http, self._settings, refresh_token)
            except Exception as e:
                # Conservatively credited, even on failure.
                self._budget.record(site_id, TOKEN_ENDPOINT, getattr(e, "http_status", 0), credited=True)
                raise
            self._budget.record(site_id, TOKEN_ENDPOINT, 200, credited=True)
            return pair

    async def _refresh_locked(self, site_id: int) -> str:
        with session_scope(self._factory) as s:
            row = s.get(SiteToken, site_id)
            if row is None:
                raise LookupError(f"no token stored for site {site_id}")
            current = self._cipher.decrypt(row.refresh_enc)
            previous = self._cipher.decrypt(row.previous_refresh_enc) if row.previous_refresh_enc else None
        used_previous = False
        try:
            try:
                pair = await self._call_refresh(site_id, current)
            except AuthExpired:
                if not previous or previous == current:
                    raise
                log.warning("refresh token rejected for site %s; retrying with previous token", site_id)
                pair = await self._call_refresh(site_id, previous)
                used_previous = True
        except AuthExpired:
            await self._mark_reauth_needed(site_id)
            raise
        self._persist_rotation(site_id, pair, keep_previous=used_previous)
        return pair.access_token

    def _persist_rotation(self, site_id: int, pair: TokenPair, keep_previous: bool) -> None:
        now = self._clock.now()
        with session_scope(self._factory) as s:
            row = s.get(SiteToken, site_id)
            if not keep_previous:  # the fallback token just worked; keep it as the fallback
                row.previous_refresh_enc = row.refresh_enc
            row.access_enc = self._cipher.encrypt(pair.access_token)
            row.refresh_enc = self._cipher.encrypt(pair.refresh_token)
            row.access_expires_at = now + timedelta(seconds=pair.expires_in)
            row.refreshed_at = now

    async def _mark_reauth_needed(self, site_id: int) -> None:
        with session_scope(self._factory) as s:
            site = s.get(Site, site_id)
            if site is None or site.status == "reauth_needed":
                return
            site.status = "reauth_needed"
        log.warning("site %s needs re-authorization", site_id)
        try:
            await self._on_reauth_needed(site_id)
        except Exception as e:
            log.error("on_reauth_needed callback failed for site %s: %s", site_id, type(e).__name__)

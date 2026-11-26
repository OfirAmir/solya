"""SolarEdge V2 monitoring client: budgeted, bearer-authenticated GETs with raw logging."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from solya.clock import Clock
from solya.config import Settings
from solya.db import SessionFactory, session_scope
from solya.models import RawResponse
from solya.solaredge.budget import CreditBudget, Priority
from solya.solaredge.errors import (
    ApiError,
    AuthExpired,
    NotFound,
    PermissionDenied,
    RateLimited,
)
from solya.solaredge.models import (
    Alert,
    Device,
    InverterState,
    LifetimeEnergy,
    Overview,
    SiteInfo,
)
from solya.solaredge.oauth import TokenManager
from solya.solaredge.parse import (
    parse_alerts,
    parse_devices,
    parse_energy_daily,
    parse_inverter_status,
    parse_lifetime,
    parse_meter_export_daily,
    parse_overview,
    parse_site,
)
from solya.solaredge.units import day_window_utc, fmt_z

log = logging.getLogger(__name__)

MAX_STATUS_WINDOW = timedelta(hours=24)
Params = list[tuple[str, str]]


def _params_json(params: Params) -> dict[str, Any]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for k, v in params:
        grouped[k].append(v)
    return {k: (v[0] if len(v) == 1 else v) for k, v in grouped.items()}


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


SITE_FIELDS = ("siteId", "name", "peakPower", "lastUpdateTime", "activationStatus")
LOCATION_FIELDS = ("timezone", "latitude", "longitude")


def _allowlist_site(body: dict) -> dict:
    out = {k: body[k] for k in SITE_FIELDS if k in body}
    loc = body.get("location")
    if isinstance(loc, dict):
        out["location"] = {k: loc[k] for k in LOCATION_FIELDS if k in loc}
    return out


class SolarEdgeClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        tokens: TokenManager,
        budget: CreditBudget,
        session_factory: SessionFactory,
        clock: Clock,
    ) -> None:
        self._http = http
        self._settings = settings
        self._tokens = tokens
        self._budget = budget
        self._factory = session_factory
        self._clock = clock

    async def _get(
        self,
        site_id: int,
        path: str,
        params: Params | None = None,
        priority: Priority = "essential",
    ) -> Any:
        params = params or []
        refreshed = rate_retried = False
        while True:
            # Token first (it may take the call lock itself on refresh), then the lock.
            token = await self._tokens.get_access_token(site_id)
            resp, body = await self._send(site_id, path, params, priority, token)
            status = resp.status_code
            if status == 401:
                if not refreshed:
                    refreshed = True
                    await self._tokens.force_refresh(site_id)
                    continue
                raise AuthExpired(f"401 on {path} after token refresh")
            try:
                return self._check(resp, path, body)
            except RateLimited as exc:
                # R22: one retry of the same call after a short Retry-After (burst limit).
                delay = None if rate_retried else self._budget.retry_delay(exc)
                if delay is None:
                    raise
                rate_retried = True
                log.info("429 on %s; retrying once in %.1f s", path, delay)
                await self._budget.sleep(delay)

    async def _send(
        self, site_id: int, path: str, params: Params, priority: Priority, token: str
    ) -> tuple[httpx.Response, Any]:
        """One budgeted GET under call_lock: acquire, HTTP, ledger record, raw row (R9)."""
        async with self._budget.call_lock:
            await self._budget.acquire(site_id, path, priority)
            try:
                resp = await self._http.get(
                    self._settings.se_api_base + path,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                self._budget.record(site_id, path, 0, False)
                raise ApiError(0, f"{type(exc).__name__} calling {path}") from None
            status = resp.status_code
            body = _json_or_none(resp)
            if path == f"/sites/{site_id}" and isinstance(body, dict):
                body = _allowlist_site(body)
            self._budget.record(site_id, path, status, 200 <= status < 300)
            self._store_raw(site_id, path, params, status, body)
        return resp, body

    async def verify_site_access(self, site_id: int, access_token: str) -> SiteInfo:
        """GET /sites/{site_id} with an explicit, not-yet-stored bearer token: proof that a
        fresh grant can read this site. One attempt, no refresh or retry. Raises on any
        non-2xx answer (403 PermissionDenied, 404 NotFound, ...)."""
        path = f"/sites/{site_id}"
        resp, body = await self._send(site_id, path, [], "essential", access_token)
        j = self._check(resp, path, body)
        return parse_site(j, self._settings.default_timezone)

    def _store_raw(self, site_id: int, path: str, params: Params, status: int, body: Any) -> None:
        with session_scope(self._factory) as s:
            s.add(
                RawResponse(
                    site_id=site_id,
                    endpoint=path,
                    params=_params_json(params),
                    status=status,
                    body=body,
                    fetched_at=self._clock.now(),
                )
            )

    @staticmethod
    def _check(resp: httpx.Response, path: str, body: Any) -> Any:
        status = resp.status_code
        if 200 <= status < 300:
            if body is None:
                raise ApiError(status, f"non-JSON response from {path}")
            return body
        if status == 403:
            raise PermissionDenied(f"403 on {path}")
        if status == 404:
            raise NotFound(f"404 on {path}")
        if status == 429:
            retry_after = None
            raw = resp.headers.get("retry-after")
            if raw is not None:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            try:
                remaining_minute = int(resp.headers.get("x-ratelimit-remaining-minute", "0"))
            except ValueError:
                remaining_minute = 0
            month_exhausted = (
                resp.headers.get("x-ratelimit-remaining-month", "").strip() == "0"
                or (remaining_minute > 0 and retry_after is None)
            )
            raise RateLimited(
                f"429 on {path}", retry_after=retry_after, month_exhausted=month_exhausted
            )
        raise ApiError(status)

    async def get_site(self, site_id: int, *, priority: Priority = "essential") -> SiteInfo:
        j = await self._get(site_id, f"/sites/{site_id}", priority=priority)
        return parse_site(j, self._settings.default_timezone)

    async def get_overview(
        self, site_id: int, local_date: date, tz: str, *, priority: Priority = "essential"
    ) -> Overview:
        start, end = day_window_utc(local_date, tz)
        j = await self._get(
            site_id, f"/sites/{site_id}/overview", [("from", start), ("to", end)], priority
        )
        return parse_overview(j, site_id)

    async def get_open_alerts(
        self, site_id: int, tz: str, *, priority: Priority = "essential"
    ) -> list[Alert]:
        j = await self._get(
            site_id,
            f"/sites/{site_id}/alerts",
            [("only-open", "true"), ("alerts-in-page", "100")],
            priority,
        )
        return parse_alerts(j, tz)

    async def get_inverter_status(
        self,
        site_id: int,
        tz: str,
        since_utc: datetime,
        until_utc: datetime,
        *,
        priority: Priority = "essential",
    ) -> dict[str, InverterState]:
        if until_utc - since_utc > MAX_STATUS_WINDOW:
            since_utc = until_utc - MAX_STATUS_WINDOW
        j = await self._get(
            site_id,
            f"/sites/{site_id}/inverters/status",
            [("resolution", "HOUR"), ("from", fmt_z(since_utc)), ("to", fmt_z(until_utc))],
            priority,
        )
        return parse_inverter_status(j, tz)

    async def get_devices(self, site_id: int, *, priority: Priority = "essential") -> list[Device]:
        j = await self._get(
            site_id,
            f"/sites/{site_id}/devices",
            [("types", "INVERTER"), ("types", "METER")],
            priority,
        )
        return parse_devices(j)

    async def get_lifetime(
        self, site_id: int, tz: str, *, priority: Priority = "essential"
    ) -> LifetimeEnergy:
        j = await self._get(site_id, f"/sites/{site_id}/lifetime-energy", priority=priority)
        return parse_lifetime(j, tz)

    @staticmethod
    def _days_params(start: date, end: date, tz: str) -> Params:
        """Site-local start of ``start`` to end of ``end``, at DAY resolution."""
        return [
            ("from", day_window_utc(start, tz)[0]),
            ("to", day_window_utc(end, tz)[1]),
            ("resolution", "DAY"),
        ]

    async def get_energy_daily(
        self, site_id: int, start: date, end: date, tz: str, *, priority: Priority = "optional"
    ) -> dict[date, float]:
        j = await self._get(
            site_id, f"/sites/{site_id}/energy", self._days_params(start, end, tz), priority
        )
        return parse_energy_daily(j, tz)

    async def get_meter_export_daily(
        self, site_id: int, start: date, end: date, tz: str, *, priority: Priority = "optional"
    ) -> dict[date, float] | None:
        """Grid export per site-local date, or None when the site has no meter."""
        try:
            j = await self._get(
                site_id,
                f"/sites/{site_id}/meters/telemetry",
                self._days_params(start, end, tz),
                priority,
            )
        except NotFound:
            return None
        return parse_meter_export_daily(j, tz)

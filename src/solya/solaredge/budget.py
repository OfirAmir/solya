import asyncio
import calendar
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select

from solya.clock import Clock
from solya.config import Settings
from solya.db import SessionFactory, session_scope
from solya.models import ApiLedger
from solya.solaredge.errors import BudgetExceeded

Priority = Literal["essential", "optional"]

OPTIONAL_FRACTION = 0.9
WINDOW_SECONDS = 60


class CreditBudget:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        clock: Clock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._clock = clock
        self._sleep = sleep

    async def acquire(self, site_id: int | None, endpoint: str, priority: Priority) -> None:
        cap = self._settings.budget_monthly_credits
        used = self.used_this_cycle()
        limit = cap * OPTIONAL_FRACTION if priority == "optional" else cap
        if used >= limit:
            raise BudgetExceeded(f"{priority} call to {endpoint} refused: {used}/{cap} credits used")
        await self._wait_for_minute_slot()

    def record(self, site_id: int | None, endpoint: str, http_status: int, credited: bool) -> None:
        with session_scope(self._factory) as s:
            s.add(
                ApiLedger(
                    ts=self._clock.now(),
                    site_id=site_id,
                    endpoint=endpoint,
                    http_status=http_status,
                    credited=credited,
                )
            )

    def used_this_cycle(self) -> int:
        return self._count_credited(self._cycle_start(self._clock.now()))

    def used_by_site(self, site_id: int, since: datetime) -> int:
        return self._count_credited(since, site_id)

    def _count_credited(self, since: datetime, site_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(ApiLedger).where(
            ApiLedger.credited.is_(True), ApiLedger.ts >= since
        )
        if site_id is not None:
            stmt = stmt.where(ApiLedger.site_id == site_id)
        with session_scope(self._factory) as s:
            return s.scalar(stmt) or 0

    def _cycle_start(self, now: datetime) -> datetime:
        day = self._settings.budget_billing_day
        now = now.astimezone(timezone.utc)
        year, month = now.year, now.month
        start = self._anniversary(year, month, day)
        if start.date() > now.date():
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
            start = self._anniversary(year, month, day)
        return start

    @staticmethod
    def _anniversary(year: int, month: int, day: int) -> datetime:
        d = min(day, calendar.monthrange(year, month)[1])
        return datetime(year, month, d, tzinfo=timezone.utc)

    async def _wait_for_minute_slot(self) -> None:
        limit = self._settings.budget_per_minute
        while True:
            now = self._clock.now()
            with session_scope(self._factory) as s:
                times = list(
                    s.scalars(
                        select(ApiLedger.ts)
                        .where(ApiLedger.ts > now - timedelta(seconds=WINDOW_SECONDS))
                        .order_by(ApiLedger.ts)
                    )
                )
            if len(times) < limit:
                return
            wait = WINDOW_SECONDS + 1 - (now - times[0]).total_seconds()
            await self._sleep(max(wait, 0.1))

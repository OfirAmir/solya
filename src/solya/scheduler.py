"""Daily job planning (pure, astral sunsets) and the APScheduler service that runs it."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from solya.clock import Clock
from solya.config import Settings
from solya.db import SessionFactory, session_scope
from solya.models import Site
from solya.rules.comms import sun_times

if TYPE_CHECKING:
    from solya.services.reporting import Reporter

log = logging.getLogger(__name__)

MISFIRE_GRACE_SECONDS = 3600
REMINDER_INTERVAL_MINUTES = 15
# A report planned late (restart, re-plan) runs this long after its owner's late pulls.
LATE_REPORT_DELAY = timedelta(minutes=5)

JobKind = Literal["evening_pull", "evening_report", "midday_check"]


@dataclass(frozen=True)
class JobSpec:
    run_at: datetime
    kind: JobKind
    target: int


def _sun(site: Site, local_date: date) -> dict[str, datetime]:
    return sun_times(site.lat, site.lon, site.tz, local_date)


def sunset_utc(site: Site, local_date: date) -> datetime:
    return _sun(site, local_date)["sunset"].astimezone(timezone.utc)


def is_daylight(site: Site, at_utc: datetime) -> bool:
    local_date = at_utc.astimezone(ZoneInfo(site.tz)).date()
    s = _sun(site, local_date)
    return s["sunrise"] <= at_utc <= s["sunset"]


def _check_time(local_date: date, hhmm: str, tz: str) -> datetime:
    hour, minute = (int(p) for p in hhmm.split(":"))
    return datetime.combine(local_date, time(hour, minute), ZoneInfo(tz)).astimezone(timezone.utc)


def plan_day_jobs(sites: list[Site], local_date: date, settings: Settings) -> list[JobSpec]:
    """Jobs for one local day. Only active sites are pulled; an owner gets a report job
    when at least one of their sites is active, timed after their latest active sunset."""
    pull_offset = timedelta(minutes=settings.evening_pull_offset_minutes)
    report_offset = timedelta(minutes=settings.report_offset_minutes)
    jobs: list[JobSpec] = []
    owner_sunsets: dict[int, list[datetime]] = defaultdict(list)

    for site in sites:
        if site.status != "active":
            continue
        sunset = sunset_utc(site, local_date)
        owner_sunsets[site.owner_id].append(sunset)
        jobs.append(JobSpec(sunset + pull_offset, "evening_pull", site.se_site_id))
        if site.commercial:
            for hhmm in site.check_times or settings.default_check_times:
                jobs.append(JobSpec(_check_time(local_date, hhmm, site.tz), "midday_check", site.se_site_id))

    for owner_id, sunsets in owner_sunsets.items():
        jobs.append(JobSpec(max(sunsets) + report_offset, "evening_report", owner_id))

    return sorted(jobs, key=lambda j: (j.run_at, j.kind, j.target))


class SchedulerService:
    def __init__(
        self,
        reporter: Reporter,
        session_factory: SessionFactory,
        settings: Settings,
        clock: Clock,
    ) -> None:
        self._reporter = reporter
        self._factory = session_factory
        self._settings = settings
        self._clock = clock
        self._tz = ZoneInfo(settings.default_timezone)  # Asia/Jerusalem by default
        self.scheduler = AsyncIOScheduler(
            timezone=timezone.utc,
            job_defaults={"misfire_grace_time": MISFIRE_GRACE_SECONDS, "coalesce": True},
        )

    def start(self) -> None:
        """Must be called from inside the running event loop."""
        self._add("plan", CronTrigger(hour=0, minute=5, timezone=self._tz), "plan_today")
        self._add(
            "send_reminders", IntervalTrigger(minutes=REMINDER_INTERVAL_MINUTES), "send_reminders"
        )
        self._add("keepalive", CronTrigger(hour=3, minute=0, timezone=self._tz), "keepalive")
        self.scheduler.start()
        self.plan_today()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def plan_today(self, owner_id: int | None = None) -> None:
        """Schedule today's jobs, for every owner or (R26) only ``owner_id``.

        Idempotent: job ids are stable and ``replace_existing`` is set.
        """
        now = self._clock.now()
        local_date = now.astimezone(self._tz).date()
        grace_cutoff = now - timedelta(seconds=MISFIRE_GRACE_SECONDS)
        with session_scope(self._factory) as s:
            stmt = select(Site).order_by(Site.se_site_id)
            if owner_id is not None:
                stmt = stmt.where(Site.owner_id == owner_id)
            sites = list(s.scalars(stmt).all())
            site_owner = {site.se_site_id: site.owner_id for site in sites}
            specs = plan_day_jobs(sites, local_date, self._settings)
        reports_due = {
            spec.target for spec in specs
            if spec.kind == "evening_report" and spec.run_at > grace_cutoff
        }
        for spec in specs:
            # R19a: keep jobs that fell due within the misfire grace (e.g. a restart
            # just after sunset) and run them now; outbox dedupe makes re-runs safe.
            # I3: a pull past the grace still runs while its owner's report is due.
            if spec.run_at <= grace_cutoff and not (
                spec.kind == "evening_pull" and site_owner[spec.target] in reports_due
            ):
                continue
            run_at = max(spec.run_at, now)
            if spec.kind == "evening_report":
                # I3: never at the same instant as a late pull; give the pulls time.
                run_at = max(spec.run_at, now + LATE_REPORT_DELAY)
            job_id = f"{spec.kind}:{spec.target}:{local_date.isoformat()}"
            args: tuple = (spec.target, local_date)
            if spec.kind == "midday_check":
                job_id += f":{spec.run_at.astimezone(self._tz):%H:%M}"
                args = (spec.target,)
            self._add(job_id, DateTrigger(run_date=run_at), spec.kind, *args)
        scope = "" if owner_id is None else f" (owner {owner_id})"
        log.info("planned %d jobs for %s%s", len(specs), local_date, scope)

    def plan_owner(self, owner_id: int) -> None:
        """R26: re-plan today's jobs for one owner, e.g. right after a site connects."""
        self.plan_today(owner_id)

    def schedule_retry(self, site_id: int, run_at: datetime) -> None:
        tz = self._settings.default_timezone
        with session_scope(self._factory) as s:
            site = s.get(Site, site_id)
            if site is not None and site.tz:
                tz = site.tz
        local_date = run_at.astimezone(ZoneInfo(tz)).date()
        self._add(
            f"retry:{site_id}:{local_date.isoformat()}",
            DateTrigger(run_date=run_at),
            "retry_pull_and_report",
            site_id,
            local_date,
        )

    def _add(self, job_id: str, trigger, method: str, *args) -> None:
        self.scheduler.add_job(
            self._run,
            trigger,
            args=(method, *args),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            coalesce=True,
        )

    async def _run(self, method: str, *args) -> None:
        """Job entry point: one failing job never affects the others (R15)."""
        try:
            if method == "plan_today":
                self.plan_today()
            else:
                await getattr(self._reporter, method)(*args)
        except Exception:
            log.exception("scheduled job %s%r failed", method, args)

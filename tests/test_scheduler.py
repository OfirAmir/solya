from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun

from solya.clock import FixedClock
from solya.db import session_scope
from solya.models import Owner, Site
from solya.scheduler import JobSpec, SchedulerService, is_daylight, plan_day_jobs, sunset_utc as plan_sunset

DAY = date(2026, 11, 24)
TZ = "Asia/Jerusalem"
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def make_site(site_id, owner_id=1, lat=32.33, lon=34.89, **kw) -> Site:
    base = dict(
        se_site_id=site_id, owner_id=owner_id, portal_name=f"p{site_id}", tz=TZ,
        lat=lat, lon=lon, status="active", commercial=False, check_times=None, monitored=True,
        created_at=NOW,
    )
    base.update(kw)
    return Site(**base)


def sunset_utc(lat, lon, d=DAY):
    return sun(Observer(lat, lon), date=d, tzinfo=ZoneInfo(TZ))["sunset"].astimezone(timezone.utc)


def of_kind(jobs, kind):
    return [j for j in jobs if j.kind == kind]


def test_plan_evening_times(settings):
    expected_sunset = sunset_utc(32.33, 34.89)
    assert abs(expected_sunset - datetime(2026, 11, 24, 14, 37, tzinfo=timezone.utc)) <= timedelta(minutes=3)

    jobs = plan_day_jobs([make_site(1)], DAY, settings)

    (pull,) = of_kind(jobs, "evening_pull")
    assert pull.target == 1
    assert pull.run_at == expected_sunset + timedelta(minutes=25)
    target = datetime(2026, 11, 24, 14, 37, tzinfo=timezone.utc) + timedelta(minutes=25)
    assert abs(pull.run_at - target) <= timedelta(minutes=3)
    (report,) = of_kind(jobs, "evening_report")
    assert report.target == 1
    assert report.run_at == expected_sunset + timedelta(minutes=30)
    assert of_kind(jobs, "midday_check") == []
    assert all(j.run_at.tzinfo is not None for j in jobs)


def test_plan_missing_location_uses_beit_yitzhak(settings):
    jobs = plan_day_jobs([make_site(1, lat=None, lon=None)], DAY, settings)
    (pull,) = of_kind(jobs, "evening_pull")
    assert pull.run_at == sunset_utc(32.33, 34.89) + timedelta(minutes=25)


def test_report_after_latest_sunset(settings):
    # A site far to the west sets later; the owner's report waits for it.
    east = make_site(1, lat=32.33, lon=34.89)
    west = make_site(2, lat=32.33, lon=30.0)
    jobs = plan_day_jobs([east, west], DAY, settings)

    pulls = {j.target: j.run_at for j in of_kind(jobs, "evening_pull")}
    assert pulls[1] == sunset_utc(32.33, 34.89) + timedelta(minutes=25)
    assert pulls[2] == sunset_utc(32.33, 30.0) + timedelta(minutes=25)
    (report,) = of_kind(jobs, "evening_report")
    assert report == JobSpec(sunset_utc(32.33, 30.0) + timedelta(minutes=30), "evening_report", 1)
    assert report.run_at > max(pulls.values())
    assert jobs == sorted(jobs, key=lambda j: j.run_at)


def test_report_one_job_per_owner(settings):
    jobs = plan_day_jobs([make_site(1, owner_id=1), make_site(2, owner_id=1), make_site(3, owner_id=2)],
                         DAY, settings)
    assert sorted(j.target for j in of_kind(jobs, "evening_report")) == [1, 2]


def test_midday_only_commercial(settings):
    home = make_site(1)
    farm = make_site(2, commercial=True)
    custom = make_site(3, commercial=True, check_times=["11:00", "15:00"])
    paused = make_site(4, commercial=True, status="paused")
    jobs = plan_day_jobs([home, farm, custom, paused], DAY, settings)

    midday = sorted((j.target, j.run_at) for j in of_kind(jobs, "midday_check"))
    jlm = ZoneInfo(TZ)
    assert midday == sorted([
        (2, datetime.combine(DAY, time(12, 30), jlm).astimezone(timezone.utc)),
        (3, datetime.combine(DAY, time(11, 0), jlm).astimezone(timezone.utc)),
        (3, datetime.combine(DAY, time(15, 0), jlm).astimezone(timezone.utc)),
    ])
    assert midday[0] == (2, datetime(2026, 11, 24, 10, 30, tzinfo=timezone.utc))


def test_reauth_site_no_pull_but_owner_report(settings):
    ok = make_site(1, owner_id=1)
    broken = make_site(2, owner_id=1, status="reauth_needed", commercial=True)
    paused_only = make_site(3, owner_id=2, status="paused")
    jobs = plan_day_jobs([ok, broken, paused_only], DAY, settings)

    assert [j.target for j in of_kind(jobs, "evening_pull")] == [1]
    assert of_kind(jobs, "midday_check") == []
    assert [j.target for j in of_kind(jobs, "evening_report")] == [1]


def test_is_daylight():
    site = make_site(1)
    s = sun(Observer(32.33, 34.89), date=DAY, tzinfo=ZoneInfo(TZ))
    assert is_daylight(site, datetime(2026, 11, 24, 10, 30, tzinfo=timezone.utc))
    assert is_daylight(site, s["sunrise"] + timedelta(minutes=1))
    assert not is_daylight(site, s["sunrise"] - timedelta(minutes=1))
    assert not is_daylight(site, s["sunset"] + timedelta(minutes=1))
    assert not is_daylight(site, datetime(2026, 11, 24, 21, 0, tzinfo=timezone.utc))


class FakeReporter:
    def __init__(self):
        self.calls = []

    async def evening_pull(self, site_id, local_date=None):
        self.calls.append(("evening_pull", site_id))

    async def evening_report(self, owner_id, local_date, retry=False):
        self.calls.append(("evening_report", owner_id))

    async def midday_check(self, site_id):
        self.calls.append(("midday_check", site_id))

    async def retry_pull_and_report(self, site_id, local_date):
        self.calls.append(("retry", site_id, local_date))

    async def send_reminders(self):
        raise RuntimeError("boom")

    async def keepalive(self):
        self.calls.append(("keepalive",))

    def backfill_soon(self, site_id):
        self.calls.append(("backfill_soon", site_id))


async def test_scheduler_service_start_smoke(db, settings):
    # A future local day so no planned job can fire while the test runs.
    day = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date() + timedelta(days=2)
    clock = FixedClock(datetime.combine(day, time(0, 10), ZoneInfo(TZ)))
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=NOW))
        s.add(make_site(1, commercial=True))
        s.add(make_site(2, status="paused"))

    svc = SchedulerService(FakeReporter(), db, settings, clock)
    svc.start()
    try:
        jobs = {j.id: j for j in svc.scheduler.get_jobs()}
        d = day.isoformat()
        assert {"plan", "send_reminders", "keepalive", "startup_backfill",
                f"evening_pull:1:{d}", f"evening_report:1:{d}", f"midday_check:1:{d}:12:30"} <= set(jobs)
        assert not any(":2:" in k for k in jobs)
        assert all(j.misfire_grace_time == 3600 and j.coalesce for j in jobs.values())

        run_at = clock.now() + timedelta(days=1)
        svc.schedule_retry(1, run_at)
        retry = [j for j in svc.scheduler.get_jobs() if j.id.startswith("retry:1:")]
        assert len(retry) == 1
        assert retry[0].next_run_time == run_at
        assert retry[0].args[0] == "retry_pull_and_report"
        assert retry[0].args[1:] == (1, run_at.astimezone(ZoneInfo(TZ)).date())

        # Re-planning is idempotent (stable ids, replace_existing).
        svc.plan_today()
        assert len(svc.scheduler.get_jobs()) == len(jobs) + 1
    finally:
        svc.shutdown()


async def test_job_runner_isolates_errors(db, settings, caplog):
    reporter = FakeReporter()
    svc = SchedulerService(reporter, db, settings, FixedClock(NOW))
    await svc._run("send_reminders")
    await svc._run("keepalive")
    assert reporter.calls == [("keepalive",)]
    assert "send_reminders" in caplog.text


async def test_restart_after_sunset_keeps_due_jobs(db, settings):
    # R19a: planning at sunset + 40 min still schedules the day's pull and report.
    day = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date() + timedelta(days=2)
    site = make_site(1)
    start = plan_sunset(site, day) + timedelta(minutes=40)
    clock = FixedClock(start)
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=NOW))
        s.add(make_site(1))

    svc = SchedulerService(FakeReporter(), db, settings, clock)
    svc.plan_today()  # scheduler not started: jobs stay pending, nothing fires
    jobs = {j.id: j for j in svc.scheduler.get_jobs()}
    d = day.isoformat()
    assert {f"evening_pull:1:{d}", f"evening_report:1:{d}"} <= set(jobs)
    assert jobs[f"evening_pull:1:{d}"].trigger.run_date == start
    # I3: the report waits for the late pull instead of racing it.
    assert jobs[f"evening_report:1:{d}"].trigger.run_date == start + timedelta(minutes=5)


async def test_plan_drops_jobs_older_than_grace(db, settings):
    day = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date() + timedelta(days=2)
    site = make_site(1)
    clock = FixedClock(plan_sunset(site, day) + timedelta(minutes=25 + 61))
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=NOW))
        s.add(make_site(1))
    svc = SchedulerService(FakeReporter(), db, settings, clock)
    svc.plan_today()
    jobs = {j.id: j for j in svc.scheduler.get_jobs()}
    d = day.isoformat()
    # The pull is 61 min overdue (beyond the grace) but the report (56 min) is still
    # due, so I3 runs the pull now and the report 5 minutes later.
    assert jobs[f"evening_pull:1:{d}"].trigger.run_date == clock.now()
    assert jobs[f"evening_report:1:{d}"].trigger.run_date == clock.now() + timedelta(minutes=5)


async def test_plan_drops_day_once_report_is_beyond_grace(db, settings):
    day = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date() + timedelta(days=2)
    site = make_site(1, commercial=True)
    clock = FixedClock(plan_sunset(site, day) + timedelta(minutes=30 + 61))
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=NOW))
        s.add(make_site(1, commercial=True))
    svc = SchedulerService(FakeReporter(), db, settings, clock)
    svc.plan_today()
    assert svc.scheduler.get_jobs() == []


async def test_startup_job_one_minute_after_start(db, settings):
    day = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ)).date() + timedelta(days=2)
    clock = FixedClock(datetime.combine(day, time(0, 10), ZoneInfo(TZ)))
    svc = SchedulerService(FakeReporter(), db, settings, clock)
    svc.start()
    try:
        job = svc.scheduler.get_job("startup_backfill")
        assert job.next_run_time == clock.now() + timedelta(minutes=1)
        assert job.args == ("startup_backfill",)
    finally:
        svc.shutdown()


async def test_startup_backfill_active_monitored_and_name_defaults(db, settings):
    with session_scope(db) as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=NOW))
        s.add(make_site(1, portal_name="אורן סופר - משק"))
        s.add(make_site(2, status="paused"))
        s.add(make_site(3, monitored=False))
        s.add(make_site(4, status="reauth_needed", card_name="x"))
    reporter = FakeReporter()
    svc = SchedulerService(reporter, db, settings, FixedClock(NOW))
    await svc._run("startup_backfill")
    assert reporter.calls == [("backfill_soon", 1)]
    with session_scope(db) as s:
        assert s.get(Site, 1).card_name
        assert s.get(Site, 4).card_name == "x"

from datetime import date, datetime, timedelta, timezone

from solya.models import DailyMetric, FaultRecord, Owner, RayDaily, Site
from solya.rules.faults import ClosedFault, DetectedFault, NewFault, sync_faults
from solya.rules.weak import WeakDay, evaluate_weak, load_weak_days, sync_weak

TODAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 15, 5, tzinfo=timezone.utc)
PARAMS = dict(threshold=70, recover=85, n_days=3, max_cloud=70)


def day(back, pr, source="history", cloud=10.0, wh=50000.0, comm="ok"):
    return WeakDay(TODAY - timedelta(days=back), pr, source, cloud, wh, comm)


def verdict(days, **kw):
    return evaluate_weak(days, TODAY, **{**PARAMS, **kw})


def test_opens_after_three_counted_low_days():
    v = verdict([day(0, 60), day(1, 62), day(2, 65)])
    assert (v.action, v.days, v.pct) == ("open", 3, 62)


def test_two_low_days_are_not_enough():
    assert verdict([day(0, 60), day(1, 62)]).action == "none"


def test_one_ok_day_among_the_last_three_blocks():
    assert verdict([day(0, 60), day(1, 75), day(2, 65), day(3, 50)]).action == "none"


def test_skips_cloudy_table_and_missing_days():
    days = [
        day(0, 60),
        day(1, 95, cloud=80.0),       # cloudy: skipped, not "good"
        day(2, 95, source="table"),   # national table: skipped
        day(3, 95, wh=None),          # no production data: skipped
        day(4, 95, wh=0.0),           # zero production has its own alert: skipped
        day(5, 95, comm="comm_loss"),  # not real data
        day(6, 95, cloud=None),       # no weather
        day(7, 64),
        day(8, 66),
    ]
    v = verdict(days)
    assert (v.action, v.days, v.pct) == ("open", 3, 63)


def test_ignores_days_older_than_ten_calendar_days():
    assert verdict([day(0, 60), day(1, 60), day(10, 60)]).action == "none"
    assert verdict([day(0, 60), day(1, 60), day(9, 60)]).action == "open"


def test_skipped_today_neither_opens_nor_closes():
    low = [day(1, 60), day(2, 60), day(3, 60)]
    assert verdict([day(0, 95, cloud=90.0)] + low).action == "none"
    assert verdict([day(0, 95, source="table")] + low).action == "none"
    assert verdict(low).action == "none"  # no row today at all


def test_recovery_closes_only_at_recover_threshold():
    assert verdict([day(0, 85), day(1, 60), day(2, 60)]).action == "close"
    assert verdict([day(0, 84.9), day(1, 60), day(2, 60)]).action == "none"
    assert verdict([day(0, 69.9), day(1, 60), day(2, 60)]).action == "open"


# ------------------------------------------------------------------ DB


def _seed(db):
    with db() as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=NOW))
        s.add(Site(se_site_id=1, owner_id=1, portal_name="p", tz="Asia/Jerusalem", created_at=NOW))
        s.commit()


def _row(s, back, pr, source="history", cloud=10.0, wh=50000.0):
    d = TODAY - timedelta(days=back)
    s.add(DailyMetric(site_id=1, local_date=d, production_wh=wh, comm_state="ok", as_of=NOW,
                      computed_at=NOW))
    s.add(RayDaily(site_id=1, local_date=d, perf_ratio=pr, expected_source=source, cloud_pct=cloud))


def test_load_weak_days_joins_metrics_and_ray(db):
    _seed(db)
    with db() as s:
        _row(s, 0, 60)
        _row(s, 11, 60)  # outside the window
        s.add(RayDaily(site_id=1, local_date=TODAY - timedelta(days=1), perf_ratio=50,
                       expected_source="history", cloud_pct=0))  # no daily_metrics row
        s.flush()
        days = load_weak_days(s, 1, TODAY, 10)
    assert [(d.local_date, d.perf_ratio, d.production_wh) for d in days] == [
        (TODAY, 60, 50000.0), (TODAY - timedelta(days=1), 50, None)]


def _settings(settings, **kw):
    return settings.model_copy(update={"weak_alerts_enabled": True, "ray_implementation": "x", **kw})


def test_sync_weak_lifecycle(db, settings):
    _seed(db)
    st = _settings(settings)
    with db() as s:
        for back in (0, 1, 2):
            _row(s, back, 60 + back)
        s.flush()
        (ev,) = sync_weak(s, 1, TODAY, NOW, st, data_complete=True)
        assert isinstance(ev, NewFault)
        rec = ev.record
        assert (rec.key, rec.source, rec.code, rec.serial) == ("weak:1", "weak", "WEAK_PRODUCTION", None)
        assert rec.detail == '{"days": 3, "pct": 61}'
        rec.notified_at = NOW
        # Same evening again: still one record.
        assert sync_weak(s, 1, TODAY, NOW, st, data_complete=True) == []

        # Next day cloudy: skipped, stays open.
        tomorrow = TODAY + timedelta(days=1)
        s.add(DailyMetric(site_id=1, local_date=tomorrow, production_wh=9000.0, comm_state="ok",
                          as_of=NOW, computed_at=NOW))
        s.add(RayDaily(site_id=1, local_date=tomorrow, perf_ratio=99, expected_source="history",
                       cloud_pct=90))
        s.flush()
        assert sync_weak(s, 1, tomorrow, NOW + timedelta(days=1), st, data_complete=True) == []
        assert rec.closed_at is None

        # A between day (75) neither closes nor re-opens.
        d2 = TODAY + timedelta(days=2)
        s.add(DailyMetric(site_id=1, local_date=d2, production_wh=40000.0, comm_state="ok",
                          as_of=NOW, computed_at=NOW))
        s.add(RayDaily(site_id=1, local_date=d2, perf_ratio=75, expected_source="history",
                       cloud_pct=5))
        s.flush()
        assert sync_weak(s, 1, d2, NOW + timedelta(days=2), st, data_complete=True) == []
        assert rec.closed_at is None

        # Recovery closes it, but not on an incomplete (silent) pull.
        d3 = TODAY + timedelta(days=3)
        s.add(DailyMetric(site_id=1, local_date=d3, production_wh=60000.0, comm_state="ok",
                          as_of=NOW, computed_at=NOW))
        s.add(RayDaily(site_id=1, local_date=d3, perf_ratio=90, expected_source="history",
                       cloud_pct=5))
        s.flush()
        assert sync_weak(s, 1, d3, NOW + timedelta(days=3), st, data_complete=False) == []
        assert rec.closed_at is None
        (closed,) = sync_weak(s, 1, d3, NOW + timedelta(days=3), st, data_complete=True)
        assert isinstance(closed, ClosedFault) and closed.record is rec


def test_sync_weak_off_when_disabled_or_ray_off(db, settings):
    _seed(db)
    with db() as s:
        for back in (0, 1, 2):
            _row(s, back, 50)
        s.flush()
        assert sync_weak(s, 1, TODAY, NOW, settings, data_complete=True) == []  # defaults: off
        off = settings.model_copy(update={"weak_alerts_enabled": True, "ray_implementation": None})
        assert sync_weak(s, 1, TODAY, NOW, off, data_complete=True) == []
        no_flag = settings.model_copy(update={"ray_implementation": "x"})
        assert sync_weak(s, 1, TODAY, NOW, no_flag, data_complete=True) == []
        assert s.query(FaultRecord).count() == 0


def test_ordinary_sync_never_closes_weak_fault(db, settings):
    """The pull's own fault sync does not know about weak production: it must leave the
    weak record open, and the weak sync must leave the other faults alone."""
    _seed(db)
    st = _settings(settings)
    with db() as s:
        for back in (0, 1, 2):
            _row(s, back, 50)
        s.flush()
        (ev,) = sync_weak(s, 1, TODAY, NOW, st, data_complete=True)
        ev.record.notified_at = NOW
        sync_faults(s, 1, [DetectedFault("alert:7", "alert", "E12", "S1", NOW)], NOW, True)
        assert sync_faults(s, 1, [DetectedFault("alert:7", "alert", "E12", "S1", NOW)], NOW, True) == []
        assert ev.record.closed_at is None
        # Weak recovery closes only the weak record.
        _row(s, -1, 95)
        s.flush()
        (closed,) = sync_weak(s, 1, TODAY + timedelta(days=1), NOW, st, data_complete=True)
        assert closed.record.key == "weak:1"
        alert = s.query(FaultRecord).filter_by(key="alert:7").one()
        assert alert.closed_at is None


def test_weak_settings_defaults(settings):
    assert settings.weak_alerts_enabled is False
    assert (settings.weak_threshold, settings.weak_recover, settings.weak_days,
            settings.weak_max_cloud_pct) == (70, 85, 3, 70)

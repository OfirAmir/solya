from datetime import datetime, timedelta, timezone

from solya.ingest import PullBundle
from solya.models import FaultRecord, Owner, Site
from solya.rules.faults import (
    ClosedFault, DetectedFault, NewFault, ack_owner_faults, detect_faults, due_reminders,
    open_fault_views, sync_faults,
)
from solya.solaredge.models import Alert, InverterState, Overview, SiteInfo

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
FRESH = NOW - timedelta(minutes=5)


def bundle(alerts=(), inverters=None, production=1000.0, last_update=FRESH):
    info = SiteInfo(1, "n", 10.0, "Asia/Jerusalem", None, None, last_update, True)
    ov = Overview(1, production, None, None, None)
    return PullBundle(info, ov, list(alerts), inverters, None, {}, NOW)


def alert(i=7):
    return Alert(i, "T", 3, "OPEN", NOW - timedelta(hours=1), None, "inv", "S1", ["E12"])


def test_detect_alert_fault():
    out = detect_faults(bundle([alert()]), [], NOW, True, "midday")
    assert out == [DetectedFault("alert:7", "alert", "E12", "S1", NOW - timedelta(hours=1))]


def test_detect_locked_mode():
    inv = {"S1": InverterState("S1", "LOCKED_STAND_ALONE", None), "S2": InverterState("S2", "MPPT", None),
           "S3": InverterState("S3", "FAULT", None)}
    keys = {f.key: f.code for f in detect_faults(bundle(inverters=inv), [], NOW, True, "midday")}
    assert keys == {"mode:S1": "LOCKED_STAND_ALONE", "mode:S3": "FAULT"}


def test_detect_missing_inverter_daylight_only():
    inv = {"S1": InverterState("S1", "MPPT", None)}
    day = detect_faults(bundle(inverters=inv), ["S1", "S2"], NOW, True, "midday")
    assert [(f.key, f.code) for f in day] == [("missing:S2", "INVERTER_NOT_REPORTING")]
    assert detect_faults(bundle(inverters=inv), ["S1", "S2"], NOW, False, "midday") == []
    assert detect_faults(bundle(inverters=None), ["S1"], NOW, True, "midday") == []


def test_zero_production_evening_fault():
    out = detect_faults(bundle(production=0.0), [], NOW, True, "evening")
    assert [(f.key, f.code) for f in out] == [("zero:2026-11-24", "ZERO_PRODUCTION")]
    assert detect_faults(bundle(production=0.0), [], NOW, True, "midday") == []
    assert detect_faults(bundle(production=0.0), [], NOW, False, "evening") == []
    assert detect_faults(bundle(production=None), [], NOW, True, "evening") == []


def test_stale_detects_nothing():
    stale = NOW - timedelta(hours=3)
    b = bundle([alert()], production=0.0, last_update=stale)
    assert detect_faults(b, [], NOW, True, "evening") is None
    no_site = PullBundle(None, None, [], {}, None, {}, NOW)
    assert detect_faults(no_site, [], NOW, True, "evening") is None


def test_stale_then_skip_keeps_fault_open(db):
    _seed(db)
    with db() as s:
        sync_faults(s, 1, [DetectedFault("alert:7", "alert", "E12", "S1", NOW)], NOW, True)
        stale = bundle([], last_update=NOW - timedelta(hours=3))
        detected = detect_faults(stale, [], NOW, True, "midday")
        if detected is not None:
            sync_faults(s, 1, detected, NOW, True)
        assert len(open_fault_views(s, 1)) == 1


def test_fault_episode_recurs(db):
    _seed(db)
    first = DetectedFault("mode:S1", "mode", "FAULT", "S1", NOW)
    again = DetectedFault("mode:S1", "mode", "FAULT", "S1", NOW + timedelta(days=1))
    with db() as s:
        ev = sync_faults(s, 1, [first], NOW, True)
        ev[0].record.notified_at = NOW
        assert [type(e) for e in sync_faults(s, 1, [], NOW + timedelta(hours=1), True)] == [ClosedFault]
        ev2 = sync_faults(s, 1, [again], NOW + timedelta(days=1), True)
        assert [type(e) for e in ev2] == [NewFault]
        assert ev2[0].record.key == "mode:S1@20261125T1500Z"
        assert ev[0].record.closed_at is not None
        assert len(open_fault_views(s, 1)) == 1
        assert sync_faults(s, 1, [again], NOW + timedelta(days=1, hours=1), True) == []


def _seed(db):
    with db() as s:
        s.add(Owner(id=1, display_name="o", telegram_chat_id=1, created_at=NOW))
        s.add(Site(se_site_id=1, owner_id=1, portal_name="p", tz="Asia/Jerusalem", created_at=NOW))
        s.commit()


def test_sync_new_then_closed(db):
    _seed(db)
    det = [DetectedFault("alert:7", "alert", "E12", "S1", NOW)]
    with db() as s:
        ev = sync_faults(s, 1, det, NOW, True)
        assert [type(e) for e in ev] == [NewFault]
        assert sync_faults(s, 1, det, NOW, True) == []
        assert len(open_fault_views(s, 1)) == 1
        ev[0].record.notified_at = NOW
        ev2 = sync_faults(s, 1, [], NOW + timedelta(hours=1), True)
        assert [type(e) for e in ev2] == [ClosedFault]
        assert open_fault_views(s, 1) == []
        old = s.query(FaultRecord).filter_by(key="alert:7").one()
        assert old.closed_at is not None


def test_sync_incomplete_data_does_not_close(db):
    _seed(db)
    with db() as s:
        sync_faults(s, 1, [DetectedFault("alert:7", "alert", "E12", "S1", NOW)], NOW, True)
        assert sync_faults(s, 1, [], NOW, False) == []
        assert len(open_fault_views(s, 1)) == 1


def test_ack_and_due_reminders(db):
    _seed(db)
    with db() as s:
        s.add(FaultRecord(site_id=1, key="alert:7", source="alert", code="E12", first_seen=NOW - timedelta(hours=5),
                          notified_at=NOW - timedelta(hours=4)))
        s.add(FaultRecord(site_id=1, key="alert:8", source="alert", code="E1", first_seen=NOW))
        s.commit()
        assert [r.key for r in due_reminders(s, NOW, 3)] == ["alert:7"]
        assert ack_owner_faults(s, 1, NOW) == 1
        assert due_reminders(s, NOW, 3) == []
        assert ack_owner_faults(s, 2, NOW) == 0

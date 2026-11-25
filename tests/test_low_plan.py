"""Low-credit call plan, the CLI flags, and scheduled pulls re-reading the site."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
import respx
from sqlalchemy import func, select

from solya.db import session_scope
from solya.models import ApiLedger, DailyMetric, FaultRecord, OutboxEntry, Site
from solya.scheduler import plan_day_jobs

from conftest import FakeChannel  # noqa: F401
from test_acceptance import BASE, DAY, FARM, HOME, NOW, c, clock, mock_site, sent  # noqa: F401
from test_cli import FETCHED, cfg, factory, run, seed_sites  # noqa: F401


def set_site(c, site_id, **fields):
    with session_scope(c.session_factory) as s:
        site = s.get(Site, site_id)
        for k, v in fields.items():
            setattr(site, k, v)


def ledger_count(c):
    with session_scope(c.session_factory) as s:
        return s.scalar(select(func.count()).select_from(ApiLedger))


async def test_low_plan_calls_only_three_endpoints(c, load_fixture):
    set_site(c, HOME, call_plan="low", devices_fetched_at=NOW, expected_inverters=["X"])
    with respx.mock:
        mock_site(load_fixture, HOME)
        await c.reporter.evening_pull(HOME, DAY)
        paths = sorted(call.request.url.path for call in respx.calls)
    assert paths == sorted(f"/v2/sites/{HOME}{p}" for p in ("", "/overview", "/alerts"))
    with session_scope(c.session_factory) as s:
        row = s.get(DailyMetric, (HOME, DAY))
        assert row.production_wh is not None
        assert row.lifetime_wh is None
        assert s.scalars(select(FaultRecord)).all() == []  # no "missing inverter" from no data


async def test_low_plan_fetches_stale_devices(c, load_fixture):
    set_site(c, HOME, call_plan="low", devices_fetched_at=NOW - timedelta(days=8))
    with respx.mock:
        mock_site(load_fixture, HOME)
        await c.reporter.evening_pull(HOME, DAY)
        paths = {call.request.url.path.removeprefix(f"/v2/sites/{HOME}") for call in respx.calls}
    assert paths == {"", "/overview", "/alerts", "/devices"}


async def test_low_plan_no_midday_job(settings, c):
    set_site(c, FARM, call_plan="low")
    with session_scope(c.session_factory) as s:
        sites = list(s.scalars(select(Site).order_by(Site.se_site_id)))
        assert all(x.commercial for x in sites if x.se_site_id == FARM)
        kinds = {(j.kind, j.target) for j in plan_day_jobs(sites, DAY, settings)}
    assert ("midday_check", FARM) not in kinds
    assert ("evening_pull", FARM) in kinds


def test_switch_to_low_closes_inverter_faults_silently(cfg, factory, capsys):
    seed_sites(factory)
    with session_scope(factory) as s:
        for key, source in (("mode:S1", "mode"), ("missing:S2", "missing"),
                            ("mode:S3@2", "mode"), ("alert:77", "alert"), ("zero:2026-11-24", "zero")):
            s.add(FaultRecord(site_id=FARM, key=key, source=source, code="C", first_seen=FETCHED,
                              notified_at=FETCHED))
        s.add(FaultRecord(site_id=HOME, key="mode:H1", source="mode", code="C", first_seen=FETCHED))
    assert run(cfg, "sites", "set", "--site", str(FARM), "--call-plan", "low") == 0
    assert "closed 3 inverter faults (low plan)" in capsys.readouterr().out
    with session_scope(factory) as s:
        by_key = {r.key: r for r in s.scalars(select(FaultRecord))}
        assert s.get(Site, FARM).call_plan == "low"
        for key in ("mode:S1", "missing:S2", "mode:S3@2"):
            assert by_key[key].closed_at is not None
            assert by_key[key].closed_notified_at is None
        assert by_key["alert:77"].closed_at is None
        assert by_key["zero:2026-11-24"].closed_at is None
        assert by_key["mode:H1"].closed_at is None  # another site
        assert s.scalar(select(func.count()).select_from(OutboxEntry)) == 0
    # Already low: nothing more to close, and going back to standard closes nothing.
    assert run(cfg, "sites", "set", "--site", str(FARM), "--call-plan", "standard") == 0
    assert "closed" not in capsys.readouterr().out


def test_cli_monitored_flag(cfg, factory):
    seed_sites(factory)
    assert run(cfg, "sites", "set", "--site", str(FARM), "--no-monitored") == 0
    with session_scope(factory) as s:
        assert s.get(Site, FARM).monitored is False
        assert s.get(Site, HOME).monitored is True
    assert run(cfg, "sites", "set", "--site", str(FARM), "--label", "x") == 0  # untouched
    with session_scope(factory) as s:
        assert s.get(Site, FARM).monitored is False
    assert run(cfg, "sites", "set", "--site", str(FARM), "--monitored") == 0
    with session_scope(factory) as s:
        assert s.get(Site, FARM).monitored is True


async def test_scheduled_pulls_skip_unmonitored_but_manual_pull_runs(c, load_fixture, caplog):
    set_site(c, FARM, monitored=False)
    caplog.set_level(logging.INFO)
    with respx.mock:
        mock_site(load_fixture, FARM)
        await c.reporter.evening_pull(FARM, DAY)
        await c.reporter.midday_check(FARM)
        assert not respx.calls
    assert ledger_count(c) == 0
    assert caplog.text.count("skip_unmonitored") == 2
    with respx.mock:
        mock_site(load_fixture, FARM)
        bundle = await c.reporter.manual_pull(FARM, "evening")
        assert respx.calls
    assert bundle is not None and bundle.overview is not None


async def test_scheduled_pulls_follow_current_call_plan(c, load_fixture):
    set_site(c, FARM, call_plan="low", devices_fetched_at=NOW, expected_inverters=["X"])
    with respx.mock:
        mock_site(load_fixture, FARM)
        await c.reporter.midday_check(FARM)  # planned earlier, plan changed since
        assert not respx.calls
        await c.reporter.evening_pull(FARM, DAY)
        paths = {call.request.url.path.removeprefix(f"/v2/sites/{FARM}") for call in respx.calls}
    assert paths == {"", "/overview", "/alerts"}


async def test_labels_count_only_monitored_sites(c, load_fixture):
    # The farm has no label of its own; the home is unmonitored. The owner therefore has
    # one monitored site, exactly as the report sees it: no site name in parentheses.
    set_site(c, FARM, label_he=None)
    set_site(c, HOME, monitored=False)
    with respx.mock:
        mock_site(load_fixture, FARM, alerts="alerts_fan.json")
        await c.reporter.evening_pull(FARM, DAY)
    faults = sent(c, "fault")
    assert faults and all("אורן סופר" not in m.text for m in faults)
    await c.reporter.on_reauth_needed(FARM)
    reauth = [m for m in sent(c, "reply") if m.dedupe_key.startswith("reauth:")]
    assert len(reauth) == 1
    assert "עבור המערכת " in reauth[0].text and "אורן סופר" not in reauth[0].text


def test_switch_to_low_reports_once_by_print_not_log(cfg, factory, capsys, caplog):
    """The CLI configures no logging, so the count is printed and not also logged."""
    seed_sites(factory)
    with session_scope(factory) as s:
        s.add(FaultRecord(site_id=FARM, key="mode:S1", source="mode", code="C", first_seen=FETCHED))
    caplog.set_level(logging.INFO)
    assert run(cfg, "sites", "set", "--site", str(FARM), "--call-plan", "low") == 0
    assert capsys.readouterr().out.count("closed 1 inverter faults (low plan)") == 1
    assert "inverter faults" not in caplog.text

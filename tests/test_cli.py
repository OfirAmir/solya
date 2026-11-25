import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select

from solya.cli import main
from solya.crypto import TokenCipher
from solya.db import SessionFactory, init_db, make_engine, session_scope
from solya.models import (
    ApiLedger,
    DailyMetric,
    FaultRecord,
    Invite,
    Owner,
    RawResponse,
    Site,
    SiteToken,
)

FARM, HOME = 3033498, 4552265
DAY = date(2026, 11, 24)
FETCHED = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
BASE = "https://monitoringapi.solaredge.com/v2"
ACCESS_TOKEN = "access-token-PLAINTEXT-123"


@pytest.fixture
def cfg(settings, tmp_path):
    return settings.model_copy(update={"database_url": f"sqlite:///{tmp_path / 'solya.db'}"})


@pytest.fixture
def factory(cfg):
    engine = make_engine(cfg.database_url)
    init_db(engine)
    yield SessionFactory(bind=engine)
    engine.dispose()


def run(cfg, *argv) -> int:
    return main(list(argv), settings=cfg)


def seed_sites(factory):
    with session_scope(factory) as s:
        s.add(Owner(id=1, display_name="אורן", telegram_chat_id=111, created_at=FETCHED))
        s.add(Site(
            se_site_id=FARM, owner_id=1, portal_name="אורן סופר", tz="Asia/Jerusalem",
            label_he="משק", tariff_ils_per_kwh=Decimal("0.45"), income_basis="production",
            peak_power_kwp_reported=65.52, created_at=FETCHED,
        ))
        s.add(Site(
            se_site_id=HOME, owner_id=1, portal_name="בית אורן", card_name="IL 360",
            tz="Asia/Jerusalem", label_he="בית", tariff_ils_per_kwh=Decimal("0.48"),
            income_basis="export", peak_power_kwp_reported=10.0, created_at=FETCHED,
        ))


def test_genkey_valid_fernet(cfg, capsys):
    assert run(cfg, "genkey") == 0
    Fernet(capsys.readouterr().out.strip().encode())


def test_invite_create_prints_both_links(cfg, factory, capsys):
    assert run(cfg, "invite", "create") == 0
    lines = capsys.readouterr().out.strip().splitlines()
    with session_scope(factory) as s:
        token = s.scalars(select(Invite.token)).one()
    base = str(cfg.public_base_url).rstrip("/")
    assert lines == [
        f"telegram: https://t.me/solya_test_bot?start={token}",
        f"web: {base}/join/{token}",
    ]
    assert "//join" not in lines[1]


def test_sites_set_updates(cfg, factory, capsys):
    seed_sites(factory)
    assert run(
        cfg, "sites", "set", "--site", str(FARM), "--label", "משק", "--tariff", "0.45",
        "--income-basis", "export", "--kwp", "90.72", "--commercial",
        "--check-times", "12:30,15:00", "--card-name", "אורן", "--status", "paused",
    ) == 0
    with session_scope(factory) as s:
        site = s.get(Site, FARM)
        assert site.label_he == "משק"
        assert site.tariff_ils_per_kwh == Decimal("0.45")
        assert site.income_basis == "export"
        assert site.billing_kwp_override == 90.72
        assert site.commercial is True
        assert site.check_times == ["12:30", "15:00"]
        assert site.card_name == "אורן"
        assert site.status == "paused"
    assert run(cfg, "sites", "set", "--site", str(FARM), "--no-commercial") == 0
    with session_scope(factory) as s:
        assert s.get(Site, FARM).commercial is False


def test_sites_set_rejects_bad_input(cfg, factory, capsys):
    seed_sites(factory)
    assert run(cfg, "sites", "set", "--site", "999", "--label", "x") == 1
    assert run(cfg, "sites", "set", "--site", str(FARM), "--check-times", "25:99") == 1
    assert run(cfg, "sites", "set", "--site", str(FARM), "--tariff", "abc") == 1


def test_sites_list_no_tokens(cfg, factory, capsys):
    seed_sites(factory)
    cipher = TokenCipher(cfg.token_encryption_key.get_secret_value())
    with session_scope(factory) as s:
        s.add(SiteToken(
            site_id=FARM, access_enc=cipher.encrypt(ACCESS_TOKEN), refresh_enc=cipher.encrypt("refresh-PLAINTEXT"),
            access_expires_at=FETCHED, refreshed_at=FETCHED, authorized_at=FETCHED, grant_expires_at=FETCHED,
        ))
    assert run(cfg, "sites", "list") == 0
    out = capsys.readouterr().out
    assert str(FARM) in out and "משק" in out and "IL 360" in out and "0.48" in out
    assert ACCESS_TOKEN not in out and "refresh-PLAINTEXT" not in out
    assert cipher.encrypt(ACCESS_TOKEN).decode() not in out


def test_owners_list_and_set_name(cfg, factory, capsys):
    seed_sites(factory)
    assert run(cfg, "owners", "set-name", "--owner", "1", "--name", "Oren") == 0
    capsys.readouterr()
    assert run(cfg, "owners", "list") == 0
    assert "Oren" in capsys.readouterr().out
    assert run(cfg, "owners", "set-name", "--owner", "9", "--name", "x") == 1


def test_credits_reports_usage(cfg, factory, capsys):
    seed_sites(factory)
    now = datetime.now(timezone.utc)
    with session_scope(factory) as s:
        for _ in range(4):
            s.add(ApiLedger(ts=now - timedelta(hours=1), site_id=FARM, endpoint="/x", http_status=200, credited=True))
        s.add(ApiLedger(ts=now - timedelta(hours=1), site_id=HOME, endpoint="/x", http_status=200, credited=False))
    assert run(cfg, "credits") == 0
    out = capsys.readouterr().out
    assert "/ 2000" in out and f"{FARM}" in out and "$0.02" in out


def test_report_dry_run_writes_png_no_http(cfg, factory, tmp_path, capsys):
    seed_sites(factory)
    with session_scope(factory) as s:
        s.add(DailyMetric(site_id=FARM, local_date=DAY, production_wh=316990.0, export_wh=316990.0,
                          income_ils=Decimal("142.65"), comm_state="ok", as_of=FETCHED, computed_at=FETCHED))
        s.add(DailyMetric(site_id=HOME, local_date=DAY, production_wh=40000.0, export_wh=25000.0,
                          income_ils=Decimal("12.00"), comm_state="ok", as_of=FETCHED, computed_at=FETCHED))
    out_dir = tmp_path / "out"
    with respx.mock(assert_all_called=False):  # no routes: any HTTP call raises
        code = run(cfg, "report", "--owner", "1", "--date", "2026-11-24", "--dry-run", "--out", str(out_dir))
    assert code == 0
    pngs = list(out_dir.glob("*.png"))
    assert len(pngs) == 1 and pngs[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    out = capsys.readouterr().out
    assert "142.65" in out and "סה״כ" in out


def test_report_requires_dry_run(cfg, factory, tmp_path):
    seed_sites(factory)
    with pytest.raises(SystemExit):
        run(cfg, "report", "--owner", "1", "--out", str(tmp_path))


def _raw(factory, load_fixture, endpoint, fixture, fetched, site=FARM, override=None):
    body = override if override is not None else load_fixture(fixture)
    with session_scope(factory) as s:
        s.add(RawResponse(site_id=site, endpoint=endpoint, params={}, status=200, body=body, fetched_at=fetched))


def test_replay_recomputes_from_raw(cfg, factory, load_fixture, capsys):
    seed_sites(factory)
    older = FETCHED - timedelta(hours=3)
    # An older, different overview must lose to the latest one of the day.
    _raw(factory, load_fixture, f"/sites/{FARM}/overview", None, older,
         override={"siteId": FARM, "production": {"total": 1000, "unit": "WH"}})
    _raw(factory, load_fixture, f"/sites/{FARM}", "solaredge/site_farm.json", FETCHED)
    _raw(factory, load_fixture, f"/sites/{FARM}/overview", "solaredge/overview_farm_20261124.json", FETCHED)
    _raw(factory, load_fixture, f"/sites/{FARM}/alerts", "solaredge/alerts_fan.json", FETCHED)
    _raw(factory, load_fixture, f"/sites/{FARM}/inverters/status", "solaredge/inverters_status_ok.json", FETCHED)
    # Another day's data must be ignored.
    _raw(factory, load_fixture, f"/sites/{FARM}/overview", None, FETCHED + timedelta(days=1),
         override={"siteId": FARM, "production": {"total": 5, "unit": "WH"}})

    with respx.mock(assert_all_called=False):
        code = run(cfg, "replay", "--site", str(FARM), "--date", "2026-11-24")
    assert code == 0
    with session_scope(factory) as s:
        row = s.get(DailyMetric, (FARM, DAY))
        assert row.production_wh == 316990.0
        assert row.income_ils == Decimal("142.65")
        assert s.scalars(select(FaultRecord)).all() == []
    out = capsys.readouterr().out
    assert "142.65" in out and "alert:101" in out and "not saved" in out


def test_replay_dry_run_writes_nothing(cfg, factory, load_fixture):
    seed_sites(factory)
    _raw(factory, load_fixture, f"/sites/{FARM}", "solaredge/site_farm.json", FETCHED)
    _raw(factory, load_fixture, f"/sites/{FARM}/overview", "solaredge/overview_farm_20261124.json", FETCHED)
    assert run(cfg, "replay", "--site", str(FARM), "--date", "2026-11-24", "--dry-run") == 0
    with session_scope(factory) as s:
        assert s.get(DailyMetric, (FARM, DAY)) is None


def test_replay_stale_says_no_faults_once(cfg, factory, load_fixture, capsys):
    seed_sites(factory)
    site = {**load_fixture("solaredge/site_farm.json"), "lastUpdateTime": "2026-11-24T08:00:00+02:00"}
    _raw(factory, load_fixture, f"/sites/{FARM}", None, FETCHED, override=site)
    _raw(factory, load_fixture, f"/sites/{FARM}/overview", "solaredge/overview_farm_20261124.json", FETCHED)
    assert run(cfg, "replay", "--site", str(FARM), "--date", "2026-11-24", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "site data is stale" in out
    assert out.count("no faults detected") == 1


def test_replay_without_stored_data_fails(cfg, factory, capsys):
    seed_sites(factory)
    assert run(cfg, "replay", "--site", str(FARM), "--date", "2026-11-24") == 1


def test_pull_prints_parsed_bundle_without_secrets(cfg, factory, load_fixture, capsys):
    cfg = cfg.model_copy(update={"budget_min_interval_seconds": 0})  # real clock: no 7.5 s waits
    seed_sites(factory)
    cipher = TokenCipher(cfg.token_encryption_key.get_secret_value())
    now = datetime.now(timezone.utc)
    with session_scope(factory) as s:
        s.add(SiteToken(
            site_id=FARM, access_enc=cipher.encrypt(ACCESS_TOKEN), refresh_enc=cipher.encrypt("refresh-PLAINTEXT"),
            access_expires_at=now + timedelta(hours=1), refreshed_at=now, authorized_at=now,
            grant_expires_at=now + timedelta(days=300),
        ))
    with respx.mock:
        respx.get(f"{BASE}/sites/{FARM}").mock(return_value=httpx.Response(200, json=load_fixture("solaredge/site_farm.json")))
        respx.get(f"{BASE}/sites/{FARM}/overview").mock(return_value=httpx.Response(200, json=load_fixture("solaredge/overview_farm_20261124.json")))
        respx.get(f"{BASE}/sites/{FARM}/alerts").mock(return_value=httpx.Response(200, json=load_fixture("solaredge/alerts_fan.json")))
        respx.get(f"{BASE}/sites/{FARM}/inverters/status").mock(return_value=httpx.Response(200, json=load_fixture("solaredge/inverters_status_ok.json")))
        respx.get(f"{BASE}/sites/{FARM}/lifetime-energy").mock(return_value=httpx.Response(200, json=load_fixture("solaredge/lifetime_farm.json")))
        respx.get(f"{BASE}/sites/{FARM}/devices").mock(return_value=httpx.Response(200, json=load_fixture("solaredge/devices_farm.json")))
        assert run(cfg, "pull", "--site", str(FARM), "--kind", "evening") == 0
    out = capsys.readouterr().out
    assert "316990" in out and "FAN_ALERT_1" in out
    assert ACCESS_TOKEN not in out
    with session_scope(factory) as s:
        assert s.scalars(select(RawResponse)).all()


PULL_NOW = datetime(2026, 11, 24, 15, 5, tzinfo=timezone.utc)


def _pull_setup(cfg, factory):
    seed_sites(factory)
    cipher = TokenCipher(cfg.token_encryption_key.get_secret_value())
    with session_scope(factory) as s:
        s.add(SiteToken(
            site_id=FARM, access_enc=cipher.encrypt(ACCESS_TOKEN), refresh_enc=cipher.encrypt("r"),
            access_expires_at=PULL_NOW + timedelta(hours=1), refreshed_at=PULL_NOW,
            authorized_at=PULL_NOW, grant_expires_at=PULL_NOW + timedelta(days=300),
        ))


def _mock_farm(load_fixture):
    for path, name in (("", "site_farm.json"), ("/overview", "overview_farm_20261124.json"),
                       ("/alerts", "alerts_fan.json"), ("/inverters/status", "inverters_status_ok.json"),
                       ("/lifetime-energy", "lifetime_farm.json"), ("/devices", "devices_farm.json")):
        respx.get(f"{BASE}/sites/{FARM}{path}").mock(
            return_value=httpx.Response(200, json=load_fixture(f"solaredge/{name}")))


def test_pull_runs_pipeline_silently_so_report_shows_the_day(cfg, factory, load_fixture, tmp_path, capsys):
    """I6/R25: `pull` persists the day and syncs faults like the scheduler, sends nothing,
    and the README's next step (`report --dry-run`) then shows the figures."""
    from solya.clock import FixedClock
    from solya.models import OutboxEntry

    cfg = cfg.model_copy(update={"budget_min_interval_seconds": 0})
    _pull_setup(cfg, factory)
    clock = FixedClock(PULL_NOW)
    with respx.mock:
        _mock_farm(load_fixture)
        assert main(["pull", "--site", str(FARM)], settings=cfg, clock=clock) == 0
    with session_scope(factory) as s:
        assert s.get(DailyMetric, (FARM, DAY)).production_wh == 316990.0
        (rec,) = s.scalars(select(FaultRecord)).all()
        assert rec.code == "FAN_ALERT_1" and rec.notified_at is None  # the service alerts later
        assert s.scalars(select(OutboxEntry)).all() == []
    capsys.readouterr()
    with respx.mock(assert_all_called=False):
        assert main(["report", "--owner", "1", "--date", "2026-11-24", "--dry-run",
                     "--out", str(tmp_path)], settings=cfg, clock=clock) == 0
    out = capsys.readouterr().out
    assert "317.0" in out and "142.65" in out


def test_pull_notify_sends(cfg, factory, load_fixture, monkeypatch):
    from conftest import FakeChannel

    import solya.app as app_mod
    from solya.clock import FixedClock

    channel = FakeChannel()
    real = app_mod.build_container
    monkeypatch.setattr(app_mod, "build_container",
                        lambda settings, clock=None: real(settings, clock=clock, channel=channel))
    cfg = cfg.model_copy(update={"budget_min_interval_seconds": 0})
    _pull_setup(cfg, factory)
    with respx.mock:
        _mock_farm(load_fixture)
        assert main(["pull", "--site", str(FARM), "--notify"], settings=cfg,
                    clock=FixedClock(PULL_NOW)) == 0
    assert [m.kind for m in channel.sent] == ["fault"]


@pytest.mark.parametrize("exc", ["invalid", "other"])
def test_pull_notify_rejected_token_not_printed(cfg, factory, monkeypatch, capsys, exc):
    """M1 on the CLI path: PTB's InvalidToken quotes the token; the command must fail
    with exit code 1 and print neither the token nor its secret half."""
    from telegram import Bot
    from telegram.error import InvalidToken, TelegramError

    async def rejected(self):
        if exc == "invalid":
            raise InvalidToken("The token `123:SECRET` was rejected by the server.")
        raise TelegramError("bad things for 123:SECRET")

    monkeypatch.setattr(Bot, "initialize", rejected)
    _pull_setup(cfg, factory)
    assert main(["pull", "--site", str(FARM), "--notify"], settings=cfg) == 1
    out, err = capsys.readouterr()
    assert "SECRET" not in out and "SECRET" not in err
    assert "TELEGRAM_BOT_TOKEN" in err

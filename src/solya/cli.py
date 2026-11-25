"""Admin CLI: ``solya <command>``. Runs on the same database and settings as the service."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from solya.clock import Clock, SystemClock
from solya.config import Settings
from solya.db import SessionFactory, init_db, make_engine, session_scope
from solya.ingest import PullBundle
from solya.models import DailyMetric, Owner, RawResponse, Site
from solya.rules.daily import compute_site_day, persist_site_day
from solya.rules.faults import detect_faults, open_fault_views
from solya.rules.ray import load_ray
from solya.services import onboarding
from solya.services.reporting import build_owner_report
from solya.solaredge.budget import CreditBudget
from solya.solaredge.parse import (
    parse_alerts,
    parse_inverter_status,
    parse_lifetime,
    parse_overview,
    parse_site,
)

USD_PER_CREDIT = 0.005
CREDITS_WINDOW = timedelta(days=30)
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class CliError(Exception):
    """A user-facing failure: printed to stderr, exit code 1."""


def _factory(settings: Settings) -> SessionFactory:
    from solya.app import _ensure_db_dir

    _ensure_db_dir(settings.database_url)
    engine = make_engine(settings.database_url)
    init_db(engine)
    return SessionFactory(bind=engine)


@contextmanager
def _session(settings: Settings):
    factory = _factory(settings)
    try:
        with session_scope(factory) as s:
            yield s
    finally:
        factory.kw["bind"].dispose()


def _cell(value) -> str:
    return "-" if value is None else str(value)


def _table(headers: list[str], rows: list[list]) -> None:
    grid = [headers] + [[_cell(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in grid) for i in range(len(headers))]
    for r in grid:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _site_or_fail(s: Session, site_id: int) -> Site:
    site = s.get(Site, site_id)
    if site is None:
        raise CliError(f"unknown site {site_id}")
    return site


# ------------------------------------------------------------------ commands


def cmd_serve(args, settings: Settings, clock: Clock) -> int:  # pragma: no cover
    from solya.app import serve

    serve(settings, args.host, args.port)
    return 0


def cmd_genkey(args, settings: Settings | None, clock: Clock) -> int:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
    return 0


def cmd_invite_create(args, settings: Settings, clock: Clock) -> int:
    with _session(settings) as s:
        token = onboarding.create_invite(s, "cli", clock.now())
    print(f"https://t.me/{settings.telegram_bot_username}?start={token}")
    return 0


def cmd_owners_list(args, settings: Settings, clock: Clock) -> int:
    with _session(settings) as s:
        rows = []
        for o in s.scalars(select(Owner).order_by(Owner.id)):
            count = len(s.scalars(select(Site.se_site_id).where(Site.owner_id == o.id)).all())
            rows.append([o.id, o.display_name, o.telegram_chat_id, o.active, count, o.last_inbound_at])
        _table(["id", "name", "chat_id", "active", "sites", "last_inbound_at"], rows)
    return 0


def cmd_owners_set_name(args, settings: Settings, clock: Clock) -> int:
    with _session(settings) as s:
        owner = s.get(Owner, args.owner)
        if owner is None:
            raise CliError(f"unknown owner {args.owner}")
        owner.display_name = args.name
    print(f"owner {args.owner} is now called {args.name}")
    return 0


SITE_COLUMNS = [
    "id", "owner", "portal_name", "label", "card_name", "commercial", "tariff",
    "basis", "status", "comm_state", "last_update_time",
]


def _site_row(site: Site) -> list:
    return [
        site.se_site_id, site.owner_id, site.portal_name, site.label_he, site.card_name,
        site.commercial, site.tariff_ils_per_kwh, site.income_basis, site.status,
        site.comm_state, site.last_update_time.isoformat() if site.last_update_time else None,
    ]


def cmd_sites_list(args, settings: Settings, clock: Clock) -> int:
    with _session(settings) as s:
        _table(SITE_COLUMNS, [_site_row(x) for x in s.scalars(select(Site).order_by(Site.se_site_id))])
    return 0


def _parse_check_times(raw: str) -> list[str]:
    times = [t.strip() for t in raw.split(",") if t.strip()]
    if not times or not all(HHMM.match(t) for t in times):
        raise CliError(f"--check-times must look like 12:30,15:00 (got {raw!r})")
    return times


def cmd_sites_set(args, settings: Settings, clock: Clock) -> int:
    with _session(settings) as s:
        site = _site_or_fail(s, args.site)
        if args.label is not None:
            site.label_he = args.label
        if args.card_name is not None:
            site.card_name = args.card_name
        if args.tariff is not None:
            try:
                site.tariff_ils_per_kwh = Decimal(args.tariff)
            except InvalidOperation:
                raise CliError(f"--tariff must be a number (got {args.tariff!r})") from None
        if args.income_basis is not None:
            site.income_basis = args.income_basis
        if args.kwp is not None:
            site.billing_kwp_override = args.kwp
        if args.commercial is not None:
            site.commercial = args.commercial
        if args.check_times is not None:
            site.check_times = _parse_check_times(args.check_times)
        if args.status is not None:
            site.status = args.status
        s.flush()
        _table(SITE_COLUMNS, [_site_row(site)])
    return 0


def cmd_credits(args, settings: Settings, clock: Clock) -> int:
    factory = _factory(settings)
    try:
        budget = CreditBudget(factory, settings, clock)
        used = budget.used_this_cycle()
        print(f"used this cycle: {used} / {settings.budget_monthly_credits} credits "
              f"(estimated ${used * USD_PER_CREDIT:.2f} at ${USD_PER_CREDIT} per credit)")
        since = clock.now() - CREDITS_WINDOW
        with session_scope(factory) as s:
            ids = list(s.scalars(select(Site.se_site_id).order_by(Site.se_site_id)))
        rows = []
        for sid in ids:
            n = budget.used_by_site(sid, since)
            rows.append([sid, n, f"${n * USD_PER_CREDIT:.2f}"])
        print("per site, last 30 days:")
        _table(["site", "credits", "est_usd"], rows)
    finally:
        factory.kw["bind"].dispose()
    return 0


def cmd_report(args, settings: Settings, clock: Clock) -> int:
    from solya.render.card import render_card

    now = clock.now()
    with _session(settings) as s:
        owner = s.get(Owner, args.owner)
        if owner is None:
            raise CliError(f"unknown owner {args.owner}")
        local_date = args.date
        if local_date is None:
            tz = s.scalars(select(Site.tz).where(Site.owner_id == owner.id)).first()
            local_date = now.astimezone(ZoneInfo(tz or settings.default_timezone)).date()
        built = build_owner_report(s, owner.id, local_date, now, settings, load_ray(settings.ray_implementation))
    if built is None:
        raise CliError(f"owner {args.owner} has no active sites")
    card, text = built
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"report_{args.owner}_{local_date.isoformat()}.png"
    png.write_bytes(render_card(card))
    print(text)
    print(f"\ncard written to {png}")
    return 0


def _local_dates_match(fetched_at: datetime, tz: str, local_date: date) -> bool:
    return fetched_at.astimezone(ZoneInfo(tz)).date() == local_date


def _latest_bodies(s: Session, site: Site, local_date: date) -> tuple[dict, datetime | None]:
    """Latest successful stored body per endpoint for the site's local ``local_date``."""
    suffixes = {
        "site": f"/sites/{site.se_site_id}",
        "overview": f"/sites/{site.se_site_id}/overview",
        "alerts": f"/sites/{site.se_site_id}/alerts",
        "inverters": f"/sites/{site.se_site_id}/inverters/status",
        "lifetime": f"/sites/{site.se_site_id}/lifetime-energy",
    }
    by_endpoint = {v: k for k, v in suffixes.items()}
    found: dict[str, RawResponse] = {}
    rows = s.scalars(
        select(RawResponse)
        .where(RawResponse.site_id == site.se_site_id, RawResponse.endpoint.in_(by_endpoint))
        .order_by(RawResponse.fetched_at.desc(), RawResponse.id.desc())
    )
    for r in rows:
        name = by_endpoint[r.endpoint]
        if name in found or not 200 <= r.status < 300 or r.body is None:
            continue
        if _local_dates_match(r.fetched_at, site.tz, local_date):
            found[name] = r
    bodies = {k: v.body for k, v in found.items()}
    anchor = found["site"].fetched_at if "site" in found else max(
        (r.fetched_at for r in found.values()), default=None
    )
    return bodies, anchor


def _bundle_from_raw(bodies: dict, site: Site, settings: Settings, pulled_at: datetime) -> PullBundle:
    errors: dict[str, str] = {}
    tz = site.tz

    def parse(name: str, fn):
        if name not in bodies:
            errors[name] = "no stored response"
            return None
        try:
            return fn(bodies[name])
        except (KeyError, ValueError, TypeError) as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            return None

    info = parse("site", lambda j: parse_site(j, settings.default_timezone))
    if info is not None:
        tz = info.timezone or tz
    return PullBundle(
        site=info,
        overview=parse("overview", lambda j: parse_overview(j, site.se_site_id)),
        alerts=parse("alerts", lambda j: parse_alerts(j, tz)),
        inverters=parse("inverters", lambda j: parse_inverter_status(j, tz)),
        lifetime=parse("lifetime", lambda j: parse_lifetime(j, tz)),
        errors=errors,
        pulled_at=pulled_at,
    )


def cmd_replay(args, settings: Settings, clock: Clock) -> int:
    local_date: date = args.date
    with _session(settings) as s:
        site = _site_or_fail(s, args.site)
        bodies, pulled_at = _latest_bodies(s, site, local_date)
        if pulled_at is None:
            raise CliError(f"no stored responses for site {args.site} on {local_date}")
        bundle = _bundle_from_raw(bodies, site, settings, pulled_at)
        for name, err in sorted(bundle.errors.items()):
            print(f"warning: {name}: {err}")
        # Same detection and day computation as an evening pull; the stored pull time is "now".
        # Faults are only printed: replay never touches fault records.
        now = pulled_at
        count = len(s.scalars(select(Site.se_site_id).where(Site.owner_id == site.owner_id)).all())
        detected = detect_faults(
            bundle, list(site.expected_inverters or []), now, True, "evening",
            settings.stale_after_minutes,
        )
        if detected is None:
            print("site data is stale: no faults detected")
        day = compute_site_day(
            site, count, bundle, open_fault_views(s, site.se_site_id), now, settings,
            load_ray(settings.ray_implementation), local_date=local_date, session=s,
        )
        lifetime_wh = bundle.lifetime.energy_wh if bundle.lifetime is not None else None
        as_of = (bundle.site.last_update_time if bundle.site is not None else None) or pulled_at
        persist_site_day(s, day, local_date, lifetime_wh, as_of, computed_at=now)
        print(f"site {site.se_site_id} {local_date}: state={day.state} production_wh={day.production_wh} "
              f"export_wh={day.export_wh} income_ils={day.income_ils} ray={day.ray}")
        for d in detected or []:
            print(f"detected fault (not saved): {d.key} ({d.code})")
        if not detected:
            print("no faults detected")
        if args.dry_run:
            s.rollback()
            print("dry run: nothing saved")
    return 0


def cmd_pull(args, settings: Settings, clock: Clock) -> int:
    """R25: the scheduler's pull pipeline (DailyMetric, faults); silent unless --notify."""
    from solya.app import build_container
    from solya.messaging.telegram import TelegramChannel

    container = build_container(settings, clock=clock)
    bot = container.bot_app.bot if args.notify and isinstance(container.channel, TelegramChannel) else None

    async def go() -> PullBundle | None:
        try:
            with session_scope(container.session_factory) as s:
                _site_or_fail(s, args.site)
            if bot is not None:
                await bot.initialize()
            return await container.reporter.manual_pull(args.site, args.kind, notify=args.notify)
        finally:
            if bot is not None:
                await bot.shutdown()
            await container.http.aclose()
            container.session_factory.kw["bind"].dispose()

    bundle = asyncio.run(go())
    if bundle is None:
        raise CliError(f"site {args.site} was not pulled (paused, needs re-authorization, "
                       "or out of credits); see the log")
    print(json.dumps(dataclasses.asdict(bundle), indent=2, ensure_ascii=False, default=str))
    print("processed like a scheduled pull: day figures and faults saved, "
          + ("messages sent" if args.notify else "no messages sent"))
    return 0


# ------------------------------------------------------------------- parser


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="solya", description="Solya admin CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="run the service")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(fn=cmd_serve)

    sub.add_parser("genkey", help="print a new Fernet key").set_defaults(fn=cmd_genkey)

    inv = sub.add_parser("invite", help="invite links").add_subparsers(dest="sub", required=True)
    inv.add_parser("create", help="print a Telegram invite link").set_defaults(fn=cmd_invite_create)

    own = sub.add_parser("owners", help="owners").add_subparsers(dest="sub", required=True)
    own.add_parser("list").set_defaults(fn=cmd_owners_list)
    sn = own.add_parser("set-name")
    sn.add_argument("--owner", type=int, required=True)
    sn.add_argument("--name", required=True)
    sn.set_defaults(fn=cmd_owners_set_name)

    sites = sub.add_parser("sites", help="sites").add_subparsers(dest="sub", required=True)
    sites.add_parser("list").set_defaults(fn=cmd_sites_list)
    ss = sites.add_parser("set")
    ss.add_argument("--site", type=int, required=True)
    ss.add_argument("--label")
    ss.add_argument("--card-name")
    ss.add_argument("--tariff", help="ILS per kWh, for example 0.45")
    ss.add_argument("--income-basis", choices=["production", "export"])
    ss.add_argument("--kwp", type=float, help="billing kWp override")
    ss.add_argument("--commercial", dest="commercial", action="store_true", default=None)
    ss.add_argument("--no-commercial", dest="commercial", action="store_false")
    ss.add_argument("--check-times", help="comma separated HH:MM local times, for example 12:30,15:00")
    ss.add_argument("--status", choices=["active", "paused"])
    ss.set_defaults(fn=cmd_sites_set)

    sub.add_parser("credits", help="API credit usage").set_defaults(fn=cmd_credits)

    rp = sub.add_parser("report", help="build an owner's report from stored data")
    rp.add_argument("--owner", type=int, required=True)
    rp.add_argument("--date", type=_date)
    rp.add_argument("--dry-run", action="store_true", required=True, help="no sending, no API calls")
    rp.add_argument("--out", default="out")
    rp.set_defaults(fn=cmd_report)

    pl = sub.add_parser(
        "pull", help="pull and process one site now, like the scheduler (live API, uses credits)")
    pl.add_argument("--site", type=int, required=True)
    pl.add_argument("--kind", choices=["evening", "midday"], default="evening")
    pl.add_argument("--notify", action="store_true",
                    help="also send the owner the alerts this pull produces (default: silent)")
    pl.set_defaults(fn=cmd_pull)

    rpl = sub.add_parser("replay", help="recompute a day from stored raw responses, no API calls")
    rpl.add_argument("--site", type=int, required=True)
    rpl.add_argument("--date", type=_date, required=True)
    rpl.add_argument("--dry-run", action="store_true", help="print only, save nothing")
    rpl.set_defaults(fn=cmd_replay)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None, clock: Clock | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if settings is None and args.fn is not cmd_genkey:  # genkey runs before .env exists
            from solya.config import get_settings

            settings = get_settings()
        return args.fn(args, settings, clock or SystemClock())
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

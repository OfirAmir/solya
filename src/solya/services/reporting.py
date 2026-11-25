"""Reporting jobs: evening pull and report, mid-day check, fault and comm alerts,
reminders, keep-alive and the connect/reauth messages.

Every outgoing text passes through ``rtl_text`` (R14) and every delivery is isolated,
so one failing owner or message never blocks the others (R15).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from solya.clock import Clock
from solya.config import Settings
from solya.db import SessionFactory, session_scope
from solya.ingest import PullBundle, pull_site
from solya.messaging.base import Channel, Ok, OutboundMessage
from solya.messaging.outbox import Outbox
from solya.models import DailyMetric, FaultRecord, OutboxEntry, Owner, Site, SiteToken
from solya.render.card import CardData, CardRow, render_card
from solya.rules.comms import is_stale
from solya.rules.daily import FaultView, SiteDay, compute_site_day, effective_label, persist_site_day
from solya.rules.faults import (
    ClosedFault,
    detect_faults,
    due_reminders,
    open_fault_views,
    sync_faults,
)
from solya.rules.ray import RayCalculator, weighted_ray
from solya.scheduler import is_daylight
from solya.services.onboarding import create_connect_request
from solya.solaredge.client import SolarEdgeClient
from solya.solaredge.errors import AuthExpired, BudgetExceeded
from solya.solaredge.oauth import TokenManager
from solya.text import he
from solya.text.rtl import rtl_text

log = logging.getLogger(__name__)

RETRY_DELAY = timedelta(minutes=30)
KEEPALIVE_AFTER = timedelta(days=7)
# The daily keep-alive job starts a little earlier than last week's run finished its
# refresh, so a strict 7-day cutoff would skip the token for a whole extra day.
KEEPALIVE_SLACK = timedelta(hours=1)
WELCOME_2_DELAY_SECONDS = 60

CARD_NOTES = {
    "comm_loss": "אין תקשורת",
    "no_data": "אין נתונים",
    "reauth_needed": "נדרש חיבור מחדש",
}

ScheduleRetry = Callable[[int, datetime], None]
DelaySend = Callable[[float, Callable[[], Awaitable[object]]], None]
# A message plus, optionally, the (fault id, column) to stamp once it is delivered.
Outgoing = tuple[OutboundMessage, tuple[int, str] | None]


def owner_chat(owner: Owner) -> str | None:
    """The Telegram recipient for ``owner``, or None while they cannot be messaged:
    a web sign-up with no linked chat yet, or an owner who blocked the bot."""
    if owner.telegram_chat_id is None or not owner.active:
        return None
    return str(owner.telegram_chat_id)


def _view(rec: FaultRecord) -> FaultView:
    return FaultView(rec.code, rec.serial, rec.first_seen, rec.source)


def _owner_sites(session: Session, owner_id: int, monitored_only: bool = False) -> list[Site]:
    stmt = select(Site).where(Site.owner_id == owner_id).order_by(Site.se_site_id)
    if monitored_only:
        stmt = stmt.where(Site.monitored.is_(True))
    return list(session.scalars(stmt))


def _skip_unlinked(what: str, owner_id: int) -> None:
    log.info("skip_unlinked: %s for owner %s not sent (no linked chat)", what, owner_id)


def _unnotified_open_faults(session: Session, site_id: int) -> list[FaultRecord]:
    return list(
        session.scalars(
            select(FaultRecord)
            .where(
                FaultRecord.site_id == site_id,
                FaultRecord.closed_at.is_(None),
                FaultRecord.notified_at.is_(None),
            )
            .order_by(FaultRecord.first_seen, FaultRecord.id)
        )
    )


def _budget_refused(bundle: PullBundle) -> bool:
    return any(e.startswith(BudgetExceeded.__name__) for e in bundle.errors.values())


def _site_id(site: Site | int) -> int:
    if isinstance(site, int):
        return site
    identity = inspect(site).identity  # readable even on a detached, expired instance
    return identity[0] if identity else site.se_site_id


def _stored_day(
    s: Session, site: Site, owner_site_count: int, local_date: date, now: datetime
) -> SiteDay:
    day = SiteDay(
        site_id=site.se_site_id,
        label=effective_label(site, owner_site_count),
        card_name=site.card_name or site.portal_name,
        state="ok",
        open_faults=open_fault_views(s, site.se_site_id),
        kwp=site.billing_kwp_override or site.peak_power_kwp_reported,
    )
    if site.status == "reauth_needed":
        day.state = "reauth_needed"
    elif site.comm_state == "loss":
        day.state = "comm_loss"
        day.comm_since = (site.comm_since or now).astimezone(ZoneInfo(site.tz))
    else:
        row = s.get(DailyMetric, (site.se_site_id, local_date))
        if row is None or row.production_wh is None:
            day.state = "no_data"
        else:
            day.production_wh = row.production_wh
            day.export_wh = row.export_wh
            day.self_consumption_wh = row.self_consumption_wh
            day.income_ils = row.income_ils
            day.ray = row.ray
    return day


def _card_row(d: SiteDay) -> CardRow:
    if d.state != "ok":
        return CardRow(d.card_name, None, None, CARD_NOTES[d.state])
    return CardRow(d.card_name, d.production_wh, d.income_ils, None)


def build_owner_report(
    session: Session,
    owner_id: int,
    local_date: date,
    now: datetime,
    settings: Settings,
    ray: RayCalculator,
) -> tuple[CardData, str] | None:
    """Card data and Hebrew text for an owner's evening report, from stored data only.

    No network access. Only monitored sites appear. Returns None when the owner has no
    monitored, unpaused site.
    """
    owner = session.get(Owner, owner_id)
    sites = _owner_sites(session, owner_id, monitored_only=True)
    shown = [site for site in sites if site.status != "paused"]
    if owner is None or not shown:
        return None
    days = [_stored_day(session, site, len(sites), local_date, now) for site in shown]
    local_dt = now.astimezone(ZoneInfo(shown[0].tz))
    show_total = len(days) > 1 and all(
        d.state == "ok" and d.production_wh is not None and d.income_ils is not None
        for d in days
    )
    total = None
    if show_total:
        total = (
            sum(d.production_wh for d in days),
            sum((d.income_ils for d in days), Decimal("0")),
        )
    card = CardData(
        ray=weighted_ray([(d.ray, d.kwp) for d in days]),
        rows=[_card_row(d) for d in days],
        total=total,
        date_label=f"{local_date:%d.%m.%Y}",
    )
    return card, he.daily_report(owner.display_name, days, local_dt, show_total)


class Reporter:
    def __init__(
        self,
        session_factory: SessionFactory,
        client: SolarEdgeClient,
        tokens: TokenManager,
        outbox: Outbox,
        channel: Channel,
        clock: Clock,
        settings: Settings,
        ray: RayCalculator,
        schedule_retry: ScheduleRetry,
        delay_send: DelaySend | None = None,
        replan_owner: Callable[[int], None] | None = None,
    ) -> None:
        self._sf = session_factory
        self._client = client
        self._tokens = tokens
        self._outbox = outbox
        self.channel = channel
        self._clock = clock
        self._settings = settings
        self._ray = ray
        self.schedule_retry = schedule_retry
        # R26: re-plans today's jobs for an owner (the running scheduler), if wired.
        self.replan_owner = replan_owner
        self._delay_send = delay_send or self._default_delay_send
        self._retry_requested: set[tuple[int, date]] = set()
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ pulls

    async def evening_pull(self, site_id: int, local_date: date | None = None) -> None:
        if self._scheduled_pull_allowed(site_id, "evening"):
            await self._pull_and_process(site_id, "evening", local_date)

    async def midday_check(self, site_id: int) -> None:
        if self._scheduled_pull_allowed(site_id, "midday"):
            await self._pull_and_process(site_id, "midday", None)

    def _scheduled_pull_allowed(self, site_id: int, kind: str) -> bool:
        """The site is re-read at run time: it may have been unmonitored, or moved to the
        low plan, after the job was planned. A manual pull does not come through here."""
        with session_scope(self._sf) as s:
            site = s.get(Site, site_id)
            if site is None:
                return True  # _pull_and_process reports the unknown site
            if not site.monitored:
                log.info("skip_unmonitored: %s pull for site %s not run", kind, site_id)
                return False
            if kind == "midday" and site.call_plan == "low":
                log.info("%s pull skipped for site %s (low call plan)", kind, site_id)
                return False
        return True

    async def manual_pull(self, site_id: int, kind: str, notify: bool = False) -> PullBundle | None:
        """R25: the admin ``pull`` command. The same pipeline as the scheduled pulls
        (DailyMetric, fault records); with ``notify`` False nothing is sent, no retry is
        scheduled, no fault is closed and the comm state is left for the service to
        change and announce.
        Returns None when the pull was skipped (unknown or inactive site, auth, budget)."""
        return await self._pull_and_process(site_id, kind, None, notify=notify)

    async def _pull_and_process(
        self, site_id: int, kind: str, local_date: date | None, notify: bool = True
    ) -> PullBundle | None:
        now = self._clock.now()
        with session_scope(self._sf) as s:
            site = s.get(Site, site_id)
            if site is None:
                log.warning("%s pull: unknown site %s", kind, site_id)
                return None
            if site.status != "active":
                log.info("%s pull skipped for site %s (status %s)", kind, site_id, site.status)
                return None
            tz = site.tz
            call_plan = site.call_plan
            # R1: the evening status window covers the whole daylight period.
            daylight = True if kind == "evening" else is_daylight(site, now)
        if local_date is None:
            local_date = now.astimezone(ZoneInfo(tz)).date()

        try:
            bundle = await pull_site(
                self._client, self._sf, self._clock, site_id, kind, local_date, call_plan
            )
        except AuthExpired:
            log.warning("site %s needs re-authorization; %s pull skipped", site_id, kind)
            return None
        except BudgetExceeded:
            log.warning("site %s: credit budget exceeded; %s pull skipped", site_id, kind)
            return None
        if bundle.errors:
            log.warning("site %s %s pull incomplete: %s", site_id, kind, sorted(bundle.errors))
        if kind == "midday" and _budget_refused(bundle):
            log.warning("site %s: credit budget exceeded; midday check skipped", site_id)
            return None

        outgoing, no_data = self._process(site_id, kind, local_date, bundle, daylight, notify)
        if not notify:
            return bundle
        for msg, stamp in outgoing:
            await self._send(msg, stamp)

        if no_data and (site_id, local_date) not in self._retry_requested:
            self._retry_requested.add((site_id, local_date))
            try:
                self.schedule_retry(site_id, self._clock.now() + RETRY_DELAY)
            except Exception:
                log.exception("could not schedule retry pull for site %s", site_id)
        return bundle

    def _process(
        self, site_id: int, kind: str, local_date: date, bundle: PullBundle, daylight: bool,
        notify: bool = True,
    ) -> tuple[list[Outgoing], bool]:
        now = self._clock.now()
        stale_after = self._settings.stale_after_minutes
        no_data = False
        with session_scope(self._sf) as s:
            site = s.get(Site, site_id)
            owner = s.get(Owner, site.owner_id)
            count = len(_owner_sites(s, owner.id, monitored_only=True))
            label = effective_label(site, count)
            tz = ZoneInfo(site.tz)
            local_now = now.astimezone(tz)

            events = []
            # An unmonitored site is only reached by a manual pull: store the data, but
            # leave its faults and comm state as they are and tell nobody.
            monitored = site.monitored
            detected = detect_faults(
                bundle, list(site.expected_inverters or []), now, daylight, kind, stale_after
            ) if monitored else None
            if detected is not None:  # R12: stale data never touches fault records
                # A silent pull never closes faults: it could not send the follow-up, and
                # nothing resends one later. The next notifying pull closes and announces it.
                data_complete = (
                    notify and "alerts" not in bundle.errors and "inverters" not in bundle.errors
                )
                events = sync_faults(s, site_id, detected, now, data_complete)

            if kind == "evening":
                day = compute_site_day(
                    site, count, bundle, open_fault_views(s, site_id), now, self._settings,
                    self._ray, local_date=local_date, session=s,
                )
                lifetime_wh = bundle.lifetime.energy_wh if bundle.lifetime is not None else None
                as_of = (bundle.site.last_update_time if bundle.site is not None else None) or bundle.pulled_at
                persist_site_day(s, day, local_date, lifetime_wh, as_of, computed_at=now)
                no_data = monitored and day.state == "no_data"

            if not monitored:
                return [], no_data
            # A silent pull leaves the transition to the service, which announces it.
            comm = self._comm_transition(site, bundle, now) if notify else None

            chat = owner_chat(owner)
            if not notify or chat is None:
                if notify and owner.telegram_chat_id is None:
                    _skip_unlinked(f"{kind} pull messages for site {site_id}", owner.id)
                return [], no_data
            name = owner.display_name
            fault_label = label if count > 1 else None
            out: list[Outgoing] = []
            # Every open, not-yet-notified fault: this pull's new ones and any whose
            # earlier alert did not get through. The dedupe key prevents a double send.
            for rec in _unnotified_open_faults(s, site_id):
                text = he.fault_alert(
                    name, fault_label, _view(rec), local_now, rec.first_seen.astimezone(tz)
                )
                out.append((OutboundMessage(chat, "fault", text, None, f"fault:{site_id}:{rec.key}"),
                            (rec.id, "notified_at")))
            for ev in events:
                if isinstance(ev, ClosedFault):
                    rec = ev.record
                    if rec.source == "alert":  # SolarEdge itself closed the alert
                        text = he.fault_closed(name, _view(rec), local_now)
                    elif rec.serial:  # R24: our own detection cleared; never "SolarEdge closed"
                        text = he.inverter_recovered(name, rec.serial, local_now)
                    else:  # ZERO_PRODUCTION: no inverter to name, close silently
                        continue
                    out.append((OutboundMessage(chat, "fault", text, None, f"closed:{site_id}:{rec.key}"),
                                (rec.id, "closed_notified_at")))
            if comm is not None:
                change, since = comm
                if change == "loss":
                    text = he.comm_loss_alert(name, label, since.astimezone(tz), local_now)
                    key = f"comm:{site_id}:{since.isoformat()}"
                else:
                    text = he.comm_restored(name, label, local_now)
                    key = f"comm_restored:{site_id}:{since.isoformat()}"
                out.append((OutboundMessage(chat, "comm_loss", text, None, key), None))
        return out, no_data

    def _comm_transition(
        self, site: Site, bundle: PullBundle, now: datetime
    ) -> tuple[str, datetime] | None:
        """Update Site.comm_state/comm_since; return ("loss"|"restored", episode start)."""
        if bundle.site is None:  # no fresh /sites answer: nothing new to conclude
            return None
        last_update = bundle.site.last_update_time
        stale = is_stale(
            last_update, now, self._settings.stale_after_minutes,
            lat=site.lat, lon=site.lon, tz=site.tz,
        )
        if stale and site.comm_state != "loss":
            site.comm_state = "loss"
            # A site that never reported has no lastUpdateTime: date the loss from the pull.
            site.comm_since = last_update or bundle.pulled_at
            return "loss", site.comm_since
        if not stale and site.comm_state == "loss":
            since = site.comm_since or now
            site.comm_state = "ok"
            site.comm_since = None
            return "restored", since
        return None

    # ----------------------------------------------------------------- report

    async def evening_report(
        self, owner_id: int, local_date: date, retry: bool = False, site_id: int | None = None
    ) -> None:
        """``retry`` re-sends after ``site_id`` recovered; the key is per site (R19b)."""
        now = self._clock.now()
        with session_scope(self._sf) as s:
            owner = s.get(Owner, owner_id)
            if owner is None or not owner.active:
                log.info("evening report skipped for inactive or unknown owner %s", owner_id)
                return
            chat = owner_chat(owner)
            if chat is None:
                _skip_unlinked(f"evening report {local_date.isoformat()}", owner_id)
                return
            built = build_owner_report(s, owner_id, local_date, now, self._settings, self._ray)
            if built is None:
                return
            card, text = built

        try:
            png = render_card(card)
        except Exception:
            log.exception("card render failed for owner %s; sending text only", owner_id)
            png = None
        key = f"report:{owner_id}:{local_date.isoformat()}"
        if retry:
            key += ":retry" + (f":{site_id}" if site_id is not None else "")
        await self._send(OutboundMessage(chat, "report", text, png, key))

    async def retry_pull_and_report(self, site_id: int, local_date: date) -> None:
        await self.evening_pull(site_id, local_date)
        with session_scope(self._sf) as s:
            site = s.get(Site, site_id)
            if site is None:
                return
            row = s.get(DailyMetric, (site_id, local_date))
            recovered = (
                site.status == "active"
                and site.comm_state != "loss"
                and row is not None
                and row.production_wh is not None
            )
            owner_id = site.owner_id
        if recovered:
            await self.evening_report(owner_id, local_date, retry=True, site_id=site_id)
        else:
            log.info("retry pull for site %s still has no data; no report re-sent", site_id)

    # ---------------------------------------------------- reminders, tokens

    async def send_reminders(self) -> None:
        now = self._clock.now()
        outgoing: list[Outgoing] = []
        with session_scope(self._sf) as s:
            for rec in due_reminders(s, now, self._settings.fault_reminder_hours):
                site = s.get(Site, rec.site_id)
                owner = s.get(Owner, site.owner_id) if site is not None else None
                chat = owner_chat(owner) if owner is not None else None
                if chat is None:
                    if owner is not None and owner.telegram_chat_id is None:
                        _skip_unlinked(f"reminder for fault {rec.id}", owner.id)
                    continue
                text = he.fault_reminder(
                    owner.display_name, _view(rec), now.astimezone(ZoneInfo(site.tz))
                )
                msg = OutboundMessage(
                    chat, "fault", text, None,
                    f"reminder:{rec.site_id}:{rec.key}",
                )
                outgoing.append((msg, (rec.id, "reminded_at")))
        for msg, stamp in outgoing:
            await self._send(msg, stamp)

    async def keepalive(self) -> None:
        """Refresh tokens idle for 7 days (paused sites included) to beat the 14-day expiry."""
        cutoff = self._clock.now() - KEEPALIVE_AFTER + KEEPALIVE_SLACK
        with session_scope(self._sf) as s:
            site_ids = list(
                s.scalars(
                    select(SiteToken.site_id)
                    .join(Site, Site.se_site_id == SiteToken.site_id)
                    .where(Site.status != "reauth_needed", SiteToken.refreshed_at < cutoff)
                    .order_by(SiteToken.site_id)
                )
            )
        for site_id in site_ids:
            try:
                await self._tokens.force_refresh(site_id)
                log.info("keep-alive refreshed token for site %s", site_id)
            except Exception as e:
                log.warning("keep-alive refresh failed for site %s: %s", site_id, type(e).__name__)

    # ------------------------------------------------------ connect, reauth

    async def on_reauth_needed(self, site_id: int) -> None:
        now = self._clock.now()
        owner_msg = None
        with session_scope(self._sf) as s:
            site = s.get(Site, site_id)
            if site is None:
                return
            local_now = now.astimezone(ZoneInfo(site.tz))
            owner = s.get(Owner, site.owner_id)
            chat = owner_chat(owner) if owner is not None else None
            if owner is not None and owner.telegram_chat_id is None:
                _skip_unlinked(f"reauth link for site {site_id}", owner.id)
            if chat is not None:
                count = len(_owner_sites(s, owner.id, monitored_only=True))
                owner_msg = self._reauth_message(s, site, owner, chat, count, now)
        if owner_msg is not None:
            await self._send(owner_msg)
        for chat_id in self._settings.admin_chat_ids:
            await self._send(
                OutboundMessage(
                    str(chat_id), "reply", f"נדרש חיבור מחדש לאתר {site_id}", None,
                    f"admin_reauth:{site_id}:{local_now.date().isoformat()}:{chat_id}",
                )
            )

    @staticmethod
    def _reauth_key(site: Site, now: datetime) -> str:
        return f"reauth:{site.se_site_id}:{now.astimezone(ZoneInfo(site.tz)).date().isoformat()}"

    def _reauth_message(
        self, s: Session, site: Site, owner: Owner, chat: str, monitored_count: int, now: datetime
    ) -> OutboundMessage:
        """A fresh connect link for ``site``; one per site per local day."""
        url = create_connect_request(s, owner.id, now, self._settings)
        return OutboundMessage(
            chat, "reply",
            he.reauth_link(owner.display_name, effective_label(site, monitored_count), url,
                           now.astimezone(ZoneInfo(site.tz))), None,
            self._reauth_key(site, now),
        )

    def _missed_while_unlinked(
        self, s: Session, owner: Owner, chat: str, monitored_count: int, now: datetime
    ) -> list[OutboundMessage]:
        """M6/M7: a reauth link or a comm-loss alert that could not be sent while the owner
        had no chat. Same dedupe keys as the live paths, so nothing is sent twice."""
        out = []
        for site in _owner_sites(s, owner.id):
            if site.status == "reauth_needed":
                sent = s.get(OutboxEntry, self._reauth_key(site, now))
                if sent is not None and sent.status == "sent":
                    continue  # no second, unused connect link for a retried link
                out.append(self._reauth_message(s, site, owner, chat, monitored_count, now))
            elif (site.status == "active" and site.monitored and site.comm_state == "loss"
                  and site.comm_since is not None):
                tz = ZoneInfo(site.tz)
                out.append(OutboundMessage(
                    chat, "comm_loss",
                    he.comm_loss_alert(owner.display_name, effective_label(site, monitored_count),
                                       site.comm_since.astimezone(tz), now.astimezone(tz)),
                    None, f"comm:{site.se_site_id}:{site.comm_since.isoformat()}",
                ))
        return out

    async def on_site_reassigned(self, site_id: int, old_owner: int, new_owner: int) -> None:
        """M4: tell the admins that a connect moved a site from one owner to another."""
        stamp = self._clock.now().isoformat()
        for chat_id in self._settings.admin_chat_ids:
            await self._send(
                OutboundMessage(
                    str(chat_id), "reply",
                    f"האתר {site_id} הועבר מבעלים {old_owner} לבעלים {new_owner}", None,
                    f"admin_reassign:{site_id}:{new_owner}:{stamp}:{chat_id}",
                )
            )

    async def on_site_connected(self, site: Site | int, is_first: bool) -> None:
        site_id = _site_id(site)
        now = self._clock.now()
        with session_scope(self._sf) as s:
            row = s.get(Site, site_id)
            owner = s.get(Owner, row.owner_id)
            chat, owner_id, portal_name = owner_chat(owner), owner.id, row.portal_name
            unlinked = owner.telegram_chat_id is None
        if self.replan_owner is not None:
            try:
                self.replan_owner(owner_id)  # tonight's pull and report for the new site
            except Exception:
                log.exception("could not re-plan today's jobs for owner %s", owner_id)
        if chat is None:
            if unlinked:  # linking the chat later sends the welcome pair instead
                _skip_unlinked(f"connect messages for site {site_id}", owner_id)
            else:
                log.info("connect messages for site %s skipped: owner %s inactive", site_id, owner_id)
            return
        await self._send(
            OutboundMessage(chat, "welcome", he.site_connected(portal_name), None,
                            f"connected:{site_id}:{now.isoformat()}")
        )
        if not is_first:
            return
        await self._send(OutboundMessage(chat, "welcome", he.WELCOME_1, None, f"welcome1:{owner_id}"))
        second = OutboundMessage(chat, "welcome", he.WELCOME_2, None, f"welcome2:{owner_id}")
        self._delay_send(WELCOME_2_DELAY_SECONDS, lambda: self._send(second))

    async def on_owner_linked(self, owner_id: int) -> None:
        """A web sign-up just linked its Telegram chat: welcome pair plus the monitored
        count, then any reauth link or comm-loss alert missed while unlinked. Dedupe keys
        make a retried /start a no-op. Unnotified open faults are left to the next pull's
        normal path (spec 5.6.1)."""
        now = self._clock.now()
        missed: list[OutboundMessage] = []
        with session_scope(self._sf) as s:
            owner = s.get(Owner, owner_id)
            chat = owner_chat(owner) if owner is not None else None
            monitored = (
                s.scalar(select(func.count()).select_from(Site).where(
                    Site.owner_id == owner_id, Site.monitored.is_(True)))
                if chat is not None else 0
            )
            if chat is not None:
                missed = self._missed_while_unlinked(s, owner, chat, monitored, now)
        if chat is None:
            return
        await self._send(OutboundMessage(chat, "welcome", he.WELCOME_1, None, f"welcome1:{owner_id}"))
        second = OutboundMessage(chat, "welcome", he.WELCOME_2, None, f"welcome2:{owner_id}")
        self._delay_send(WELCOME_2_DELAY_SECONDS, lambda: self._send(second))
        if monitored:
            await self._send(OutboundMessage(
                chat, "welcome", he.sites_monitored(monitored), None, f"monitored:{owner_id}"))
        for msg in missed:
            await self._send(msg)

    # -------------------------------------------------------------- delivery

    def _default_delay_send(
        self, seconds: float, coro_factory: Callable[[], Awaitable[object]]
    ) -> None:
        async def later() -> None:
            await asyncio.sleep(seconds)
            await coro_factory()

        task = asyncio.get_running_loop().create_task(later())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _send(self, msg: OutboundMessage, stamp: tuple[int, str] | None = None) -> bool:
        """Deliver one message; never raises. Stamps the fault column only on success."""
        msg.text = rtl_text(msg.text)
        try:
            result = await self._outbox.deliver(self.channel, msg)
        except Exception:
            log.exception("delivery of %s failed", msg.dedupe_key)
            return False
        if not isinstance(result, Ok):
            log.warning("delivery of %s not completed: %s", msg.dedupe_key, type(result).__name__)
            return False
        if stamp is not None:
            fault_id, column = stamp
            try:
                with session_scope(self._sf) as s:
                    rec = s.get(FaultRecord, fault_id)
                    if rec is not None and getattr(rec, column) is None:
                        setattr(rec, column, self._clock.now())
            except Exception:
                log.exception("could not record %s for fault %s", column, fault_id)
        return True

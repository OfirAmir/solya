"""Fault detection (pure) and fault record lifecycle (DB)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from solya.ingest import PullBundle
from solya.models import FaultRecord, Site
from solya.rules.comms import is_stale
from solya.rules.daily import FaultView
from solya.solaredge.parse import is_fault_mode


def _base(key: str) -> str:
    return key.split("@")[0]


@dataclass(frozen=True)
class DetectedFault:
    key: str
    source: str
    code: str
    serial: str | None
    first_seen: datetime
    # Source-specific figures for the alert text (JSON), e.g. weak production's days/pct.
    detail: str | None = None


# Evaluated by its own evening check (rules/weak.py), never by a pull's detection.
SEPARATE_SOURCES = frozenset({"weak"})


@dataclass(frozen=True)
class NewFault:
    record: FaultRecord


@dataclass(frozen=True)
class ClosedFault:
    record: FaultRecord


FaultEvent = NewFault | ClosedFault


def detect_faults(
    bundle: PullBundle,
    expected_inverters: list[str],
    now: datetime,
    is_daylight: bool,
    kind: str,
    stale_after_minutes: int = 60,
    min_fault_samples: int = 2,
) -> list[DetectedFault] | None:
    """Faults visible in one pull. ``is_daylight`` is decided by the caller.

    An inverter fault mode counts only when at least ``min_fault_samples`` hourly
    samples of the window were a fault mode (FAULT_MIN_SAMPLES): one sample is a blip.
    SolarEdge alerts, a missing inverter and zero production are not gated.

    Returns None when the site is stale or unknown: no inference is possible, so the
    caller must skip ``sync_faults`` and leave open faults open.
    """
    if bundle.site is None:
        return None
    info = bundle.site
    if is_stale(
        info.last_update_time, now, stale_after_minutes,
        lat=info.lat, lon=info.lon, tz=info.timezone,
    ):
        return None

    found: list[DetectedFault] = []

    for alert in bundle.alerts or []:
        code = alert.error_codes[0] if alert.error_codes else (alert.type or "ALERT")
        found.append(
            DetectedFault(
                key=f"alert:{alert.alert_id}",
                source="alert",
                code=code,
                serial=alert.serial,
                first_seen=alert.first_trigger or now,
            )
        )

    if bundle.inverters is not None:
        for serial, state in bundle.inverters.items():
            # R24: any fault sample in the window (midnight to now), not only the last.
            # The detection time stays the pull time (acceptance a7).
            mode, samples = state.fault_mode, state.fault_samples
            if mode is None and is_fault_mode(state.last_mode):
                mode, samples = state.last_mode, max(samples, 1)
            if mode is not None and samples >= min_fault_samples:
                found.append(DetectedFault(f"mode:{serial}", "mode", mode, serial, now))
        if is_daylight:
            for serial in expected_inverters:
                if serial not in bundle.inverters:
                    found.append(
                        DetectedFault(
                            f"missing:{serial}", "missing", "INVERTER_NOT_REPORTING", serial, now
                        )
                    )

    if (
        kind == "evening"
        and is_daylight
        and bundle.overview is not None
        and bundle.overview.production_wh == 0.0
    ):
        tz = ZoneInfo(bundle.site.timezone)
        local_date = now.astimezone(tz).date()
        found.append(
            DetectedFault(f"zero:{local_date.isoformat()}", "zero", "ZERO_PRODUCTION", None, now)
        )

    return found


def sync_faults(
    session: Session,
    site_id: int,
    detected: list[DetectedFault],
    now: datetime,
    data_complete: bool,
    source: str | None = None,
) -> list[FaultEvent]:
    """Reconcile detected faults with stored records. A closed fault seen again opens a
    new episode under a new key (R13); a closed key itself is never reopened.

    ``source`` None reconciles a pull's detection: it never closes a record of a
    separately evaluated source (weak production). A ``source`` name closes only that
    source's records."""
    rows = session.scalars(select(FaultRecord).where(FaultRecord.site_id == site_id)).all()
    keys = {r.key for r in rows}
    open_bases = {_base(r.key) for r in rows if r.closed_at is None}
    seen_bases = {_base(r.key) for r in rows}
    events: list[FaultEvent] = []

    detected_bases = set()
    for d in detected:
        base = _base(d.key)
        detected_bases.add(base)
        if base in open_bases:
            continue
        key = d.key
        if base in seen_bases:  # a new episode of a closed fault
            key = f"{base}@{d.first_seen.astimezone(timezone.utc):%Y%m%dT%H%MZ}"
        if key in keys:
            continue
        rec = FaultRecord(
            site_id=site_id,
            key=key,
            source=d.source,
            code=d.code,
            serial=d.serial,
            first_seen=d.first_seen,
            detail=d.detail,
        )
        session.add(rec)
        keys.add(key)
        open_bases.add(base)
        events.append(NewFault(rec))

    def managed(rec: FaultRecord) -> bool:
        return rec.source == source if source is not None else rec.source not in SEPARATE_SOURCES

    if data_complete:
        for rec in rows:
            if rec.closed_at is None and managed(rec) and _base(rec.key) not in detected_bases:
                rec.closed_at = now
                if rec.notified_at is not None:
                    events.append(ClosedFault(rec))

    session.flush()
    return events


def open_fault_views(session: Session, site_id: int) -> list[FaultView]:
    rows = session.scalars(
        select(FaultRecord)
        .where(FaultRecord.site_id == site_id, FaultRecord.closed_at.is_(None))
        .order_by(FaultRecord.first_seen, FaultRecord.id)
    ).all()
    return [FaultView(r.code, r.serial, r.first_seen, r.source, r.detail) for r in rows]


def ack_owner_faults(session: Session, owner_id: int, now: datetime) -> int:
    site_ids = select(Site.se_site_id).where(Site.owner_id == owner_id)
    rows = session.scalars(
        select(FaultRecord).where(
            FaultRecord.site_id.in_(site_ids),
            FaultRecord.notified_at.is_not(None),
            FaultRecord.acked_at.is_(None),
            FaultRecord.closed_at.is_(None),
        )
    ).all()
    for r in rows:
        r.acked_at = now
    session.flush()
    return len(rows)


def due_reminders(session: Session, now: datetime, hours: float) -> list[FaultRecord]:
    cutoff = now - timedelta(hours=hours)
    return list(
        session.scalars(
            select(FaultRecord).where(
                FaultRecord.notified_at.is_not(None),
                FaultRecord.notified_at <= cutoff,
                FaultRecord.reminded_at.is_(None),
                FaultRecord.acked_at.is_(None),
                FaultRecord.closed_at.is_(None),
            )
        ).all()
    )

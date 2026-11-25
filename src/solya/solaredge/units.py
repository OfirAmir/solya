from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_WH = {"WH": 1.0, "KWH": 1e3, "MWH": 1e6, "GWH": 1e9}
_W = {"W": 1.0, "KW": 1e3, "MW": 1e6}


def _convert(value: float | None, unit: str | None, table: dict[str, float]) -> float | None:
    if value is None:
        return None
    factor = table.get((unit or "").strip().upper())
    if factor is None:
        raise ValueError(f"unknown unit: {unit!r}")
    return float(value) * factor


def to_wh(value: float | None, unit: str | None) -> float | None:
    return _convert(value, unit, _WH)


def to_w(value: float | None, unit: str | None) -> float | None:
    return _convert(value, unit, _W)


def parse_se_time(s: str, tz: str) -> datetime:
    """Parse a SolarEdge timestamp to UTC. Bare timestamps are site-local."""
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc)


def fmt_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_window_utc(local_date: date, tz: str) -> tuple[str, str]:
    """Local midnight to next local midnight, as ISO Z strings (DST-safe)."""
    zone = ZoneInfo(tz)
    start = datetime.combine(local_date, time.min, tzinfo=zone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    return fmt_z(start), fmt_z(end)

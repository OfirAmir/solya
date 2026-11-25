"""Communication freshness rule."""
from __future__ import annotations

from datetime import datetime, timedelta


def is_stale(last_update_time: datetime | None, now: datetime, stale_after_minutes: int) -> bool:
    """True when the site has not reported within the window; unknown counts as stale."""
    if last_update_time is None:
        return True
    return now - last_update_time > timedelta(minutes=stale_after_minutes)

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, dt: datetime) -> None:
        self._now = self._check(dt)

    @staticmethod
    def _check(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        return dt.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, dt: datetime) -> None:
        self._now = self._check(dt)

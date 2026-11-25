"""Base protocol and shared date/HTTP helpers for interest connectors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

DEFAULT_TIMEOUT_SECONDS = 10.0


class InterestConnector(Protocol):
    """Interface implemented by every personal signal source connector."""

    name: str

    def fetch_signals(self, lookback_days: int) -> list["SignalItem"]:
        """Fetch recent signals within the given lookback window."""
        ...


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


def cutoff_datetime(lookback_days: int, now: datetime | None = None) -> datetime:
    """Compute the earliest UTC timestamp included in the lookback window."""
    ref = now or utc_now()
    return ref - timedelta(days=lookback_days)


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 date/datetime string into a UTC-aware datetime."""
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


from the_reading.interest.models import SignalItem  # noqa: E402, F401

"""Time helpers.

Time helpers should produce timezone-aware UTC values for tokens, audit
timestamps, idempotency expiration, and request metrics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(UTC)


def utc_now_plus(**kwargs: float) -> datetime:
    """Return a timezone-aware UTC datetime offset by a timedelta."""
    return utc_now() + timedelta(**kwargs)


def to_iso8601(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

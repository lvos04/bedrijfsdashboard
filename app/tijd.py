"""Tijdhulpen: alles intern in UTC, ISO 8601 met 'Z'."""

from __future__ import annotations

from datetime import UTC, datetime


def nu() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    dt = dt or nu()
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(waarde: str | None) -> datetime | None:
    if not waarde or not isinstance(waarde, str):
        return None
    try:
        dt = datetime.fromisoformat(waarde.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def leesbaar(waarde: str | None) -> str:
    """Korte Nederlandse weergave in lokale tijd van de server (TZ-omgevingsvariabele)."""
    dt = parse_iso(waarde)
    if dt is None:
        return "onbekend"
    return dt.astimezone().strftime("%d-%m-%Y %H:%M")

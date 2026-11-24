"""Єдине джерело часу (UTC). У тестах підміняється через `set_now`."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

UTC = timezone.utc
_now: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731


def now() -> datetime:
    return _now()


def set_now(fn: Callable[[], datetime] | None) -> None:
    """Підмінити годинник (тести). None — повернути системний."""
    global _now
    _now = fn or (lambda: datetime.now(UTC))


def iso(dt: datetime) -> str:
    """UTC ISO-рядок, що коректно сортується як текст у SQLite."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime заборонено — передай часовий пояс")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def now_iso() -> str:
    return iso(now())

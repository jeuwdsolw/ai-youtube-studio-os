"""Контроль витрат: сесії Claude Code, знаки ElevenLabs, одиниці квоти YouTube API.

limit = 0 означає «ліміт не задано» (облік ведеться, блоку немає).
"""
from __future__ import annotations

import sqlite3

from . import clock, events
from .db import tx
from ..config import Config


def period_key(cfg: Config, period: str) -> str:
    d = clock.now().astimezone(cfg.tz).date()
    if period == "day":
        return d.isoformat()
    if period == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{d:%Y-%m}"


def usage(con: sqlite3.Connection, cfg: Config, resource: str) -> tuple[int, int]:
    b = cfg.budgets.get(resource)
    if not b:
        return 0, 0
    row = con.execute("SELECT used FROM budget_usage WHERE resource=? AND period=?",
                      (resource, period_key(cfg, b.period))).fetchone()
    return (row["used"] if row else 0), b.limit


def allowed(con: sqlite3.Connection, cfg: Config, resource: str, amount: int = 1) -> bool:
    used, limit = usage(con, cfg, resource)
    return limit <= 0 or used + amount <= limit


def next_period_utc(cfg: Config, period: str):
    """Початок наступного періоду бюджету (місцева північ / понеділок / 1-ше число) в UTC."""
    from datetime import datetime, time as dtime, timedelta
    d = clock.now().astimezone(cfg.tz).date()
    if period == "day":
        n = d + timedelta(days=1)
    elif period == "week":
        n = d + timedelta(days=7 - d.weekday())
    else:
        n = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return datetime.combine(n, dtime(0, 5), tzinfo=cfg.tz).astimezone(clock.now().tzinfo or None)


def require(con: sqlite3.Connection, cfg: Config, resource: str, amount: int = 1, what: str = "") -> None:
    """Бюджет вичерпано → задача відкладається до нового періоду (LimitError: спроба не витрачається), а не «потрібна людина»."""
    if allowed(con, cfg, resource, amount):
        return
    from ..errors import LimitError
    used, limit = usage(con, cfg, resource)
    until = next_period_utc(cfg, cfg.budgets[resource].period)
    raise LimitError(f"бюджет {resource} вичерпано ({used}/{limit}){' — ' + what if what else ''}; продовження — {until:%Y-%m-%d %H:%M}",
                     until)


def spend(con: sqlite3.Connection, cfg: Config, resource: str, amount: int, *, video_id: str | None = None) -> int:
    b = cfg.budgets.get(resource)
    if not b:
        raise KeyError(f"бюджет {resource!r} не описано в config/studio.toml [budgets]")
    key = period_key(cfg, b.period)
    with tx(con):
        con.execute(
            "INSERT INTO budget_usage(resource, period, used) VALUES (?,?,?) "
            "ON CONFLICT(resource, period) DO UPDATE SET used = used + excluded.used",
            (resource, key, amount),
        )
        used = con.execute("SELECT used FROM budget_usage WHERE resource=? AND period=?", (resource, key)).fetchone()[0]
        if b.limit and used > b.limit:
            events.emit(con, "warning", "budget", f"{resource}: перевищено ліміт {used}/{b.limit} ({key})", video_id=video_id)
    return used

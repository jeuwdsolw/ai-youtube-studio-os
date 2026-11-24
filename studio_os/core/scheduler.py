"""Календар публікацій і виробничі дедлайни.

Час слотів задано як місцевий (Europe/Kyiv); UTC обчислюється для кожної дати окремо, тому перехід
на літній/зимовий час враховано автоматично. Відео створюються заздалегідь на `plan_horizon_weeks`.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import clock, events
from .db import tx
from ..config import STAGES, Config


def local_to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Місцевий «настінний» час → UTC. Неіснуючий час (весняний перехід) зсувається вперед,
    неоднозначний (осінній) береться перший (fold=0)."""
    if naive.tzinfo is not None:
        raise ValueError("очікується naive datetime місцевого часу")
    aware = naive.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(clock.UTC)
    back = utc.astimezone(tz).replace(tzinfo=None)
    if back != naive:  # неіснуючий час: стрілки переведено вперед
        utc = (naive + timedelta(hours=1)).replace(tzinfo=tz).astimezone(clock.UTC)
    return utc


def video_id(fmt: str, local: datetime) -> str:
    return f"{'S' if fmt == 'short' else 'L'}{local:%Y%m%d-%H%M}"


def slots_between(cfg: Config, start: date, end: date) -> list[tuple[str, datetime, datetime]]:
    """Усі слоти [start, end): (format, місцевий naive, UTC), відсортовані за часом."""
    out = []
    d = start
    while d < end:
        for s in cfg.slots:
            if s.weekday == d.weekday():
                local = datetime.combine(d, s.at)
                out.append((s.format, local, local_to_utc(local, cfg.tz)))
        d += timedelta(days=1)
    return sorted(out, key=lambda x: (x[2], x[0]))


def stage_deadline(cfg: Config, fmt: str, stage: str, local_publish: datetime) -> datetime | None:
    days = cfg.deadlines.get(fmt, {}).get(stage)
    if days is None:
        return None
    return local_to_utc(local_publish - timedelta(days=days), cfg.tz)


def plan(con: sqlite3.Connection, cfg: Config, *, start: date | None = None, weeks: int | None = None) -> list[str]:
    """Створити слоти календаря на горизонт планування (ідемпотентно). Повертає ID нових відео."""
    now_utc = clock.now()
    start = start or now_utc.astimezone(cfg.tz).date()
    end = start + timedelta(weeks=weeks if weeks is not None else cfg.plan_horizon_weeks)
    created = []
    ts = clock.iso(now_utc)
    with tx(con):
        for fmt, local, utc in slots_between(cfg, start, end):
            if utc <= now_utc:
                continue
            vid = video_id(fmt, local)
            cur = con.execute(
                "INSERT OR IGNORE INTO videos(id, format, publish_at_utc, publish_local, tz, priority, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (vid, fmt, clock.iso(utc), local.strftime("%Y-%m-%d %H:%M"), cfg.tz.key,
                 40 if fmt == "long" else 50, ts, ts),
            )
            if not cur.rowcount:
                continue
            for seq, st in enumerate(STAGES):
                dl = stage_deadline(cfg, fmt, st, local)
                con.execute(
                    "INSERT INTO stages(video_id, stage, seq, deadline_utc, updated_at) VALUES (?,?,?,?,?)",
                    (vid, st, seq, clock.iso(dl) if dl else None, ts),
                )
            created.append(vid)
        if created:
            events.emit(con, "info", "scheduler", f"заплановано {len(created)} відео: {created[0]} … {created[-1]}")
    return created


def check_deadlines(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Попередити (один раз) про етапи, що прострочили дедлайн; позначити відео, що не встигли до публікації."""
    now = clock.now_iso()
    late = []
    with tx(con):
        rows = con.execute(
            "SELECT s.video_id, s.stage, s.deadline_utc FROM stages s JOIN videos v ON v.id = s.video_id "
            "WHERE s.status NOT IN ('done','skipped') AND s.deadline_utc IS NOT NULL AND s.deadline_utc < ? "
            "AND s.warned_late = 0 AND v.status NOT IN ('published','cancelled')",
            (now,),
        ).fetchall()
        for r in rows:
            con.execute("UPDATE stages SET warned_late = 1 WHERE video_id=? AND stage=?", (r["video_id"], r["stage"]))
            events.emit(con, "warning", "scheduler", f"етап «{r['stage']}» прострочив дедлайн {r['deadline_utc']}",
                        video_id=r["video_id"])
            late.append((r["video_id"], r["stage"]))
        missed = con.execute(
            "SELECT id FROM videos WHERE publish_at_utc < ? AND status NOT IN ('published','cancelled','late')", (now,)
        ).fetchall()
        for m in missed:
            con.execute("UPDATE videos SET status='late', updated_at=? WHERE id=?", (now, m["id"]))
            events.emit(con, "error", "scheduler", "час публікації минув, відео не опубліковано — автоматично не публікується",
                        video_id=m["id"])
    return late

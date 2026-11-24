"""AGENT 2 — Content Strategist (етап `planning` кожного відео).

Детермінований вибір теми зі звіту трендів (без LLM — дешево й передбачувано):
- формат теми збігається зі слотом (або both);
- найвищий бал мінус штраф за жанр, що вже був серед останніх N відео цього формату (різноманітність, ТЗ §5);
- тема, яку власник відхилив (reject planning), більше не пропонується.
Тему, задану власником вручну (`topic <video> "…"`), стратег не чіпає.
"""
from __future__ import annotations

from ..core import clock
from ..core.db import tx
from .base import Agent, AgentResult, BlockedError, Context, register

GENRE_PENALTY = 15


def recent_genres(con, fmt: str, before_utc: str, n: int) -> list[str]:
    rows = con.execute(
        "SELECT t.genre FROM topics t JOIN videos v ON v.id = t.assigned_video "
        "WHERE v.format = ? AND v.publish_at_utc < ? AND t.status IN ('assigned','used') "
        "ORDER BY v.publish_at_utc DESC LIMIT ?", (fmt, before_utc, n)).fetchall()
    return [r["genre"] for r in rows]


def pick(con, video, window: int):
    fmt = video["format"]
    genres = recent_genres(con, fmt, video["publish_at_utc"], window)
    cands = con.execute(
        "SELECT * FROM topics WHERE status = 'new' AND format_rec IN (?, 'both') ORDER BY total DESC, id",
        (fmt,)).fetchall()
    best, best_score = None, None
    for t in cands:
        s = t["total"] - (GENRE_PENALTY if t["genre"] in genres else 0)
        if best_score is None or s > best_score:
            best, best_score = t, s
    return best, genres


@register
class ContentStrategist(Agent):
    name = "planning"
    stage = "planning"

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        con, vid = ctx.con, ctx.video_id
        v = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        st = con.execute("SELECT note FROM stages WHERE video_id=? AND stage='planning'", (vid,)).fetchone()
        rejected = bool(st and st["note"] and st["note"].startswith("доопрацювати"))
        cur = con.execute("SELECT * FROM topics WHERE assigned_video=? AND status='assigned'", (vid,)).fetchone()
        if cur and not rejected:
            return AgentResult(summary=f"тема вже призначена: {cur['title']}", data={"topic_id": cur["id"]})
        if v["topic"] and not cur and not rejected:
            return AgentResult(summary=f"тема задана власником: {v['topic']}", data={"topic_id": None, "manual": True})
        with tx(con):
            if cur:
                con.execute("UPDATE topics SET status='rejected' WHERE id=?", (cur["id"],))
            t, genres = pick(con, v, int(ctx.cfg.production.get("recent_genre_window", 5)))
            if t is None:
                raise BlockedError(f"немає вільних тем формату {v['format']} — потрібен новий звіт трендів (research)")
            con.execute("UPDATE topics SET status='assigned', assigned_video=? WHERE id=?", (vid, t["id"]))
            con.execute("UPDATE videos SET topic=?, title=?, updated_at=? WHERE id=?",
                        (t["title"], t["title_idea"], clock.now_iso(), vid))
        note = f"жанр «{t['genre']}»" + (" (повтор жанру — інших тем немає)" if t["genre"] in genres else "")
        return AgentResult(summary=f"тема: {t['title']} — {t['total']}/100, {note}",
                           data={"topic_id": t["id"], "score": t["total"], "genre": t["genre"], "hook": t["hook"]})

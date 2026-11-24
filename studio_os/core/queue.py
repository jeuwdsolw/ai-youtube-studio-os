"""Довговічна черга задач на SQLite.

- ідемпотентність: унікальний `key` (повторний enqueue повертає наявну задачу);
- оренда (lease): задача `running` належить воркеру до `lease_until`; heartbeat продовжує її;
  після падіння процесу прострочена оренда повертає задачу в чергу (`recover_expired`);
- повтори з експоненційною затримкою; після `max_attempts` — статус `dead` (потрібна людина);
- ексклюзивні ресурси (`remotion`, `blender`, `claude`…): дві задачі з одним ресурсом не виконуються одночасно.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from . import clock, events
from .db import tx

BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 3600


def backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** max(0, attempt - 1)))


def enqueue(con: sqlite3.Connection, kind: str, key: str, payload: dict | None = None, *,
            video_id: str | None = None, stage: str | None = None, priority: int = 50,
            run_after: str | None = None, max_attempts: int = 3, resources: list[str] | tuple = ()) -> int:
    now = clock.now_iso()
    with tx(con):
        row = con.execute("SELECT id FROM jobs WHERE key = ?", (key,)).fetchone()
        if row:
            return row["id"]
        cur = con.execute(
            "INSERT INTO jobs(kind, key, payload, video_id, stage, priority, run_after, max_attempts, resources,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kind, key, json.dumps(payload or {}, ensure_ascii=False), video_id, stage, priority,
             run_after or now, max_attempts, json.dumps(sorted(set(resources))), now, now),
        )
        jid = cur.lastrowid
        events.emit(con, "info", "queue", f"enqueue {kind} ({key})", video_id=video_id, job_id=jid)
        return jid


def _locked(con: sqlite3.Connection, now: str) -> set[str]:
    return {r["resource"] for r in con.execute("SELECT resource FROM locks WHERE lease_until >= ?", (now,))}


def claim(con: sqlite3.Connection, worker: str, *, kinds: set[str] | None = None,
          lease_seconds: int = 900, exclusive: frozenset[str] | set[str] = frozenset()) -> sqlite3.Row | None:
    """Взяти найпріоритетнішу готову задачу, чиї ексклюзивні ресурси вільні."""
    now_dt = clock.now()
    now = clock.iso(now_dt)
    until = clock.iso(now_dt + timedelta(seconds=lease_seconds))
    with tx(con):
        con.execute("DELETE FROM locks WHERE lease_until < ?", (now,))
        busy = _locked(con, now)
        rows = con.execute(
            "SELECT * FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY priority, run_after, id",
            (now,),
        ).fetchall()
        for job in rows:
            if kinds is not None and job["kind"] not in kinds:
                continue
            need = set(json.loads(job["resources"])) & set(exclusive)
            if need & busy:
                continue
            con.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, lease_owner=?, lease_until=?, updated_at=? "
                "WHERE id=? AND status='queued'",
                (worker, until, now, job["id"]),
            )
            for r in need:
                con.execute("INSERT INTO locks(resource, job_id, lease_until) VALUES (?,?,?)", (r, job["id"], until))
            if job["stage"] and job["video_id"]:
                con.execute("UPDATE stages SET status='running', attempts=attempts+1, updated_at=? "
                            "WHERE video_id=? AND stage=?", (now, job["video_id"], job["stage"]))
            return con.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    return None


def heartbeat(con: sqlite3.Connection, job_id: int, worker: str, lease_seconds: int = 900) -> bool:
    until = clock.iso(clock.now() + timedelta(seconds=lease_seconds))
    with tx(con):
        n = con.execute("UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND status='running' AND lease_owner=?",
                        (until, clock.now_iso(), job_id, worker)).rowcount
        if n:
            con.execute("UPDATE locks SET lease_until=? WHERE job_id=?", (until, job_id))
    return bool(n)


def complete(con: sqlite3.Connection, job_id: int, worker: str, result: dict | None = None) -> bool:
    now = clock.now_iso()
    with tx(con):
        n = con.execute(
            "UPDATE jobs SET status='done', result=?, lease_owner=NULL, lease_until=NULL, last_error=NULL, updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (json.dumps(result or {}, ensure_ascii=False), now, job_id, worker),
        ).rowcount
        con.execute("DELETE FROM locks WHERE job_id=?", (job_id,))
        if n:
            events.emit(con, "info", "queue", "done", job_id=job_id)
    return bool(n)


def _fail_locked(con: sqlite3.Connection, job: sqlite3.Row, error: str, retry: bool) -> str:
    now_dt = clock.now()
    attempts = job["attempts"]
    if retry and attempts < job["max_attempts"]:
        status, run_after = "queued", clock.iso(now_dt + backoff(attempts))
        level = "warning"
    else:
        status, run_after = "dead", job["run_after"]
        level = "error"
    con.execute(
        "UPDATE jobs SET status=?, run_after=?, last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
        (status, run_after, error[:4000], clock.iso(now_dt), job["id"]),
    )
    con.execute("DELETE FROM locks WHERE job_id=?", (job["id"],))
    if job["stage"] and job["video_id"]:
        st = "queued" if status == "queued" else "needs_human"
        # повтор: зауваження власника «доопрацювати: …» не затираємо помилкою (агент має його побачити)
        con.execute("UPDATE stages SET status=?, note=CASE WHEN ?='queued' AND note LIKE 'доопрацювати%' THEN note ELSE ? END, "
                    "updated_at=? WHERE video_id=? AND stage=?",
                    (st, st, error[:500], clock.iso(now_dt), job["video_id"], job["stage"]))
    msg = (f"спроба {attempts}/{job['max_attempts']} невдала, повтор після {run_after}: {error[:300]}"
           if status == "queued" else f"задачу зупинено після {attempts} спроб — потрібна людина: {error[:300]}")
    events.emit(con, level, "queue", msg, video_id=job["video_id"], job_id=job["id"])
    return status


def fail(con: sqlite3.Connection, job_id: int, worker: str, error: str, *, retry: bool = True) -> str | None:
    with tx(con):
        job = con.execute("SELECT * FROM jobs WHERE id=? AND status='running' AND lease_owner=?",
                          (job_id, worker)).fetchone()
        if not job:
            return None
        return _fail_locked(con, job, error, retry)


def defer(con: sqlite3.Connection, job_id: int, worker: str, until: str, reason: str) -> bool:
    """Ліміт (Claude/квота): повернути задачу в чергу до `until`, спробу не зараховувати, примітку етапу не чіпати."""
    with tx(con):
        job = con.execute("SELECT * FROM jobs WHERE id=? AND status='running' AND lease_owner=?", (job_id, worker)).fetchone()
        if not job:
            return False
        now = clock.now_iso()
        con.execute("UPDATE jobs SET status='queued', attempts=MAX(0, attempts-1), run_after=?, last_error=?, "
                    "lease_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?", (until, reason[:4000], now, job_id))
        con.execute("DELETE FROM locks WHERE job_id=?", (job_id,))
        if job["stage"] and job["video_id"]:
            con.execute("UPDATE stages SET status='queued', attempts=MAX(0, attempts-1), updated_at=? "
                        "WHERE video_id=? AND stage=?", (now, job["video_id"], job["stage"]))
        events.emit(con, "warning", "queue", f"ліміт — задачу відкладено до {until} (спробу не зараховано): {reason[:200]}",
                    video_id=job["video_id"], job_id=job_id)
    return True


def recover_expired(con: sqlite3.Connection) -> list[int]:
    """Задачі з простроченою орендою (процес упав/перезапуск) → повтор або dead."""
    now = clock.now_iso()
    out = []
    with tx(con):
        for job in con.execute("SELECT * FROM jobs WHERE status='running' AND lease_until < ?", (now,)).fetchall():
            _fail_locked(con, job, "оренда задачі прострочена (процес зупинився або перезапуск)", retry=True)
            out.append(job["id"])
    return out


def cancel(con: sqlite3.Connection, job_id: int, reason: str = "скасовано") -> bool:
    with tx(con):
        n = con.execute("UPDATE jobs SET status='cancelled', last_error=?, lease_owner=NULL, lease_until=NULL, "
                        "updated_at=? WHERE id=? AND status IN ('queued','running','failed','dead')",
                        (reason, clock.now_iso(), job_id)).rowcount
        con.execute("DELETE FROM locks WHERE job_id=?", (job_id,))
        if n:
            events.emit(con, "warning", "queue", f"cancel: {reason}", job_id=job_id)
    return bool(n)


def retry_dead(con: sqlite3.Connection, job_id: int) -> bool:
    """Людина вирішила проблему — повернути dead-задачу в чергу з новим лічильником."""
    with tx(con):
        n = con.execute("UPDATE jobs SET status='queued', attempts=0, run_after=?, updated_at=? "
                        "WHERE id=? AND status IN ('dead','failed','cancelled')",
                        (clock.now_iso(), clock.now_iso(), job_id)).rowcount
        if n:
            events.emit(con, "info", "queue", "повернуто в чергу вручну", job_id=job_id)
    return bool(n)

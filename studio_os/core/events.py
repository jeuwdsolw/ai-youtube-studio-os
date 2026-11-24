"""Журнал операцій: кожна важлива подія — у таблицю `events` (для дашборду) і у файл `data/logs/studio.log`."""
from __future__ import annotations

import json
import logging
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import clock

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
           "error": logging.ERROR, "critical": logging.CRITICAL}
log = logging.getLogger("studio")


def setup_file_log(log_dir: Path) -> None:
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(log_dir / "studio.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.DEBUG)


def emit(con: sqlite3.Connection, level: str, source: str, message: str, *,
         video_id: str | None = None, job_id: int | None = None, data: dict | None = None) -> None:
    if level not in _LEVELS:
        raise ValueError(level)
    con.execute(
        "INSERT INTO events(ts, level, source, video_id, job_id, message, data) VALUES (?,?,?,?,?,?,?)",
        (clock.now_iso(), level, source, video_id, job_id, message,
         json.dumps(data, ensure_ascii=False) if data else None),
    )
    ctx = " ".join(x for x in (video_id and f"[{video_id}]", job_id and f"#job{job_id}") if x)
    log.log(_LEVELS[level], "%s %s %s", source, ctx, message)


def recent(con: sqlite3.Connection, limit: int = 50, min_level: str = "debug") -> list[sqlite3.Row]:
    lv = [k for k, v in _LEVELS.items() if v >= _LEVELS[min_level]]
    q = f"SELECT * FROM events WHERE level IN ({','.join('?' * len(lv))}) ORDER BY id DESC LIMIT ?"
    return con.execute(q, (*lv, limit)).fetchall()

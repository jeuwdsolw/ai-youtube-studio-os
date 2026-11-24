"""SQLite-сховище стану студії: схема, міграції, підключення, резервні копії.

Творчий вміст (сценарії, розкадровки, код планів) лишається у файлах епізодів і git;
тут — стан конвеєра, черга задач, журнал, бюджети, налаштування.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import clock

# Міграції застосовуються по черзі; номер = PRAGMA user_version. Існуючі НЕ редагувати — лише додавати нові.
MIGRATIONS: list[str] = [
    # 1 — ядро (Етап 2)
    """
    CREATE TABLE videos (
        id              TEXT PRIMARY KEY,             -- S20261130-1800 / L20261201-1730
        format          TEXT NOT NULL CHECK (format IN ('short','long')),
        publish_at_utc  TEXT NOT NULL,
        publish_local   TEXT NOT NULL,                -- '2026-11-30 18:00' у часовому поясі tz
        tz              TEXT NOT NULL,
        topic           TEXT,
        title           TEXT,
        priority        INTEGER NOT NULL DEFAULT 50,
        episode_dir     TEXT,                         -- episodes/<ep> після створення
        source_video_id TEXT REFERENCES videos(id),   -- Shorts-нарізка з Long
        status          TEXT NOT NULL DEFAULT 'planned'
                        CHECK (status IN ('planned','in_production','ready','published','cancelled','late')),
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX videos_publish ON videos(publish_at_utc);

    CREATE TABLE stages (
        video_id     TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        stage        TEXT NOT NULL,
        seq          INTEGER NOT NULL,
        status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','queued','running','awaiting_approval','done',
                                       'failed','blocked','needs_human','skipped')),
        deadline_utc TEXT,
        attempts     INTEGER NOT NULL DEFAULT 0,
        note         TEXT,
        result       TEXT,                            -- JSON від агента
        approved_by  TEXT,
        warned_late  INTEGER NOT NULL DEFAULT 0,
        updated_at   TEXT NOT NULL,
        PRIMARY KEY (video_id, stage)
    );

    CREATE TABLE jobs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        kind         TEXT NOT NULL,                   -- ім'я агента/задачі
        key          TEXT NOT NULL UNIQUE,            -- ідемпотентність
        payload      TEXT NOT NULL DEFAULT '{}',
        video_id     TEXT REFERENCES videos(id) ON DELETE SET NULL,
        stage        TEXT,
        status       TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','done','failed','dead','cancelled')),
        priority     INTEGER NOT NULL DEFAULT 50,     -- менше = раніше
        run_after    TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        resources    TEXT NOT NULL DEFAULT '[]',      -- JSON-список ексклюзивних ресурсів
        lease_owner  TEXT,
        lease_until  TEXT,
        last_error   TEXT,
        result       TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX jobs_ready ON jobs(status, run_after, priority);

    CREATE TABLE locks (
        resource    TEXT PRIMARY KEY,
        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        lease_until TEXT NOT NULL
    );

    CREATE TABLE events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT NOT NULL,
        level    TEXT NOT NULL CHECK (level IN ('debug','info','warning','error','critical')),
        source   TEXT NOT NULL,
        video_id TEXT,
        job_id   INTEGER,
        message  TEXT NOT NULL,
        data     TEXT
    );
    CREATE INDEX events_ts ON events(ts);

    CREATE TABLE settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE budget_usage (
        resource TEXT NOT NULL,
        period   TEXT NOT NULL,                       -- '2026-11-23' / '2026-W48' / '2026-11'
        used     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (resource, period)
    );
    """,
    # 2 — дослідження й теми (Етап 3)
    """
    CREATE TABLE trend_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT NOT NULL,
        week        TEXT NOT NULL,
        sources     TEXT NOT NULL,                    -- JSON: які джерела були доступні / ні й чому
        summary     TEXT,
        data        TEXT                              -- JSON: агреговані показники, запити, квота
    );

    CREATE TABLE trend_videos (
        report_id       INTEGER NOT NULL REFERENCES trend_reports(id) ON DELETE CASCADE,
        yt_id           TEXT NOT NULL,
        query           TEXT,
        title           TEXT,
        channel_id      TEXT,
        channel_title   TEXT,
        channel_subs    INTEGER,                      -- NULL, якщо приховано
        published_at    TEXT,
        age_days        REAL,
        views           INTEGER,
        likes           INTEGER,                      -- NULL, якщо приховано
        comments        INTEGER,
        duration_s      INTEGER,
        format          TEXT,                         -- short | long
        views_per_day   REAL,
        outlier         REAL,                         -- перегляди / підписники каналу (NULL без даних)
        PRIMARY KEY (report_id, yt_id)
    );

    CREATE TABLE topics (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        report_id        INTEGER REFERENCES trend_reports(id) ON DELETE SET NULL,
        title            TEXT NOT NULL,
        description      TEXT,
        why              TEXT,
        evidence         TEXT,                        -- JSON: yt_id відео-доказів (лише з trend_videos)
        plot_options     TEXT,
        scores           TEXT NOT NULL,               -- JSON: 7 критеріїв (null = немає даних)
        total            INTEGER NOT NULL,
        format_rec       TEXT CHECK (format_rec IN ('short','long','both')),
        title_idea       TEXT,
        hook             TEXT,
        genre            TEXT,
        status           TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','assigned','used','rejected')),
        assigned_video   TEXT REFERENCES videos(id) ON DELETE SET NULL,
        created_at       TEXT NOT NULL
    );
    CREATE INDEX topics_status ON topics(status, total);
    """,
    # 3 — контроль якості (Етап 4)
    """
    CREATE TABLE qc_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id    TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        round       INTEGER NOT NULL,
        score       REAL NOT NULL,
        verdict     TEXT NOT NULL,
        critical    INTEGER NOT NULL DEFAULT 0,
        report      TEXT NOT NULL,                    -- JSON: модулі, проблеми, виміри, не перевірене
        path        TEXT,                             -- qc/qc-rN.md у теці епізоду
        created_at  TEXT NOT NULL
    );
    CREATE INDEX qc_reports_video ON qc_reports(video_id, round);

    CREATE TABLE fix_tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id     TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        qc_report_id INTEGER REFERENCES qc_reports(id) ON DELETE CASCADE,
        stage        TEXT NOT NULL,
        scope        TEXT,
        severity     TEXT NOT NULL,
        description  TEXT NOT NULL,
        criterion    TEXT,
        status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','sent','resolved','human')),
        created_at   TEXT NOT NULL
    );
    CREATE INDEX fix_tasks_video ON fix_tasks(video_id, status);
    """,
    # 4 — публікація YouTube (Етап 5): стан завантаження, щоб повтор задачі не створив дубль відео
    """
    CREATE TABLE uploads (
        video_id     TEXT PRIMARY KEY,
        channel_id   TEXT,
        session_uri  TEXT,                               -- resumable upload: продовження з місця обриву
        yt_video_id  TEXT,
        publish_at   TEXT,
        thumbnail    TEXT,                               -- ok / помилка
        captions     TEXT,
        updated_at   TEXT NOT NULL
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    """Підключення з WAL, зовнішніми ключами й очікуванням блокувань. isolation_level=None — транзакції вручну."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
    return con


def migrate(con: sqlite3.Connection) -> int:
    """Застосувати нові міграції. Повертає поточну версію схеми."""
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[ver:], start=ver + 1):
        con.execute("BEGIN IMMEDIATE")
        try:
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                con.execute(stmt)
            con.execute(f"PRAGMA user_version = {i}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return con.execute("PRAGMA user_version").fetchone()[0]


def open_db(path: Path | str) -> sqlite3.Connection:
    con = connect(path)
    migrate(con)
    return con


class tx:
    """Транзакція з негайним записним блокуванням: `with tx(con): ...`."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, *_):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# ---- налаштування (key → JSON) ----
def get_setting(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), clock.now_iso()),
    )


# ---- резервні копії ----
def backup(con: sqlite3.Connection, backup_dir: Path, keep: int) -> Path:
    """Онлайн-копія через sqlite backup API (безпечно під час роботи). Тримає `keep` останніх."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"studio-{clock.now().strftime('%Y%m%d-%H%M%S')}.db"
    out = sqlite3.connect(str(dst))
    try:
        con.backup(out)
    finally:
        out.close()
    olds = sorted(backup_dir.glob("studio-*.db"))
    for old in olds[:-keep] if keep > 0 else []:
        old.unlink()
    return dst

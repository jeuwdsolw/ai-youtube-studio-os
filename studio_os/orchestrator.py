"""Головний координатор: розклад → етапи → черга → агенти → погодження.

Один процес; стан повністю в SQLite, тому перезапуск безпечний (прострочені оренди повертаються в чергу).
Режими (config [modes.*]): manual — етапи запускає людина й погоджує кожен; semi — агенти працюють самі,
людина погоджує теми, сценарії, фінал; auto — усе саме, публікація лише після QC і з дозволеним auto_publish.
STOP: `python -m studio_os pause` або файл data/STOP — нові задачі не стартують, довгі агенти зупиняються між кроками.
"""
from __future__ import annotations

import json
from datetime import timedelta
import os
import shutil
import socket
import sqlite3
import threading

import traceback

from . import agents  # noqa: F401 — реєстрація агентів
from .agents.base import REGISTRY, AgentResult, BlockedError, Context, RetryableError, for_stage
from .errors import LimitError
from .config import STAGES, Config
from .core import clock, db, events, queue, scheduler
from .core.db import tx

QC_PASS = ("APPROVED", "APPROVED_WITH_WARNINGS")
HEAVY = {"remotion", "blender"}


class Orchestrator:
    def __init__(self, con: sqlite3.Connection, cfg: Config, worker_id: str | None = None):
        self.con = con
        self.cfg = cfg
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    # ---------- режим і STOP ----------
    @property
    def mode_name(self) -> str:
        return db.get_setting(self.con, "mode", self.cfg.mode)

    @property
    def mode(self):
        return self.cfg.mode_rules(self.mode_name)

    def set_mode(self, name: str) -> None:
        self.cfg.mode_rules(name)  # KeyError, якщо невідомий
        with tx(self.con):
            db.set_setting(self.con, "mode", name)
            events.emit(self.con, "warning", "orchestrator", f"режим змінено на {name}")

    @property
    def stop_file(self):
        return self.cfg.db_path.parent / "STOP"

    def paused(self) -> bool:
        return bool(db.get_setting(self.con, "paused", False)) or self.stop_file.exists()

    def pause(self, reason: str = "") -> None:
        with tx(self.con):
            db.set_setting(self.con, "paused", True)
            events.emit(self.con, "warning", "orchestrator", f"STOP: студію призупинено {reason}".strip())
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(reason or "paused", encoding="utf-8")

    def resume(self) -> None:
        with tx(self.con):
            db.set_setting(self.con, "paused", False)
            events.emit(self.con, "warning", "orchestrator", "студію відновлено")
        if self.stop_file.exists():
            self.stop_file.unlink()

    # ---------- цикл ----------
    def tick(self) -> dict:
        out = {"recovered": queue.recover_expired(self.con)}
        out["planned"] = scheduler.plan(self.con, self.cfg)
        out["late"] = scheduler.check_deadlines(self.con)
        out["backup"] = self._daily_backup()
        out["paused"] = self.paused()
        out["enqueued"] = [] if out["paused"] else self.advance()
        return out

    def _daily_backup(self) -> str | None:
        today = clock.now().astimezone(self.cfg.tz).date().isoformat()
        if db.get_setting(self.con, "last_backup_day") == today:
            return None
        path = db.backup(self.con, self.cfg.backup_dir, self.cfg.backups_keep)
        with tx(self.con):
            db.set_setting(self.con, "last_backup_day", today)
        return str(path)

    # ---------- етапи ----------
    def _stages(self, vid: str) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM stages WHERE video_id=? ORDER BY seq", (vid,)).fetchall()

    def current_stage(self, vid: str) -> sqlite3.Row | None:
        for s in self._stages(vid):
            if s["status"] not in ("done", "skipped"):
                return s
        return None

    def qc_verdict(self, vid: str) -> str | None:
        row = self.con.execute("SELECT status, result FROM stages WHERE video_id=? AND stage='qc'", (vid,)).fetchone()
        if not row or row["status"] != "done" or not row["result"]:
            return None
        res = json.loads(row["result"])
        if res.get("critical"):
            return "BLOCKED_CRITICAL"
        return res.get("verdict")

    def can_publish(self, vid: str) -> tuple[bool, str]:
        """Критичні помилки QC (зламаний файл, чорний екран, тиша) блокують завжди. Вердикти нижче порога
        (NEEDS_REVISION/REJECTED) — публікація лише після РУЧНОГО погодження: власник бачив звіт і вирішив, що «піде»."""
        verdict = self.qc_verdict(vid)
        if verdict is None or verdict == "BLOCKED_CRITICAL":
            return False, f"QC не пройдено ({verdict or 'немає звіту'}) — публікація заблокована"
        appr = self.con.execute("SELECT status, approved_by FROM stages WHERE video_id=? AND stage='approval'",
                                (vid,)).fetchone()
        if not appr or appr["status"] != "done":
            return False, "немає погодження фінального відео"
        if verdict not in QC_PASS and appr["approved_by"] in (None, "auto"):
            return False, f"QC {verdict}: публікація лише після ручного погодження"
        if appr["approved_by"] == "auto" and not self.mode.auto_publish:
            return False, "автопублікацію не дозволено (modes.<режим>.auto_publish = false)"
        return True, ""

    _KEEP = object()

    def _set_stage(self, vid: str, stage: str, status: str, note=_KEEP, **extra) -> None:
        """note не передано → примітку (напр. «доопрацювати: …» від власника) зберігаємо для агента."""
        if note is not Orchestrator._KEEP:
            extra = {"note": note, **extra}
        sets = ["status=?", "updated_at=?"] + [f"{k}=?" for k in extra]
        self.con.execute(f"UPDATE stages SET {', '.join(sets)} WHERE video_id=? AND stage=?",
                         (status, clock.now_iso(), *extra.values(), vid, stage))

    def _enqueue_stage(self, vid: str, stage: str, priority: int) -> int:
        cls = for_stage(stage)
        n = self.con.execute("SELECT COUNT(*) FROM jobs WHERE video_id=? AND stage=?", (vid, stage)).fetchone()[0]
        jid = queue.enqueue(self.con, cls.name, f"{vid}:{stage}:r{n + 1}", {"video_id": vid},
                            video_id=vid, stage=stage, priority=priority,
                            max_attempts=cls.max_attempts, resources=cls.resources)
        with tx(self.con):
            self._set_stage(vid, stage, "queued")
            self.con.execute("UPDATE videos SET status='in_production', updated_at=? WHERE id=? AND status='planned'",
                             (clock.now_iso(), vid))
        return jid

    def _active_count(self) -> int:
        """Відео у виробництві, що зараз рухаються (етап у роботі або в черзі). Застряглі на людині не займають місце."""
        n = 0
        for (vid,) in self.con.execute("SELECT id FROM videos WHERE status='in_production'").fetchall():
            st = self.current_stage(vid)
            if st and st["status"] in ("pending", "queued", "running"):
                n += 1
        return n

    def advance(self, *, manual: bool = False, only: str | None = None) -> list[int]:
        """Просунути конвеєр: для кожного відео — перший незавершений етап."""
        if self.paused():
            return []
        if not manual and not self.mode.auto_start:
            return []
        out = []
        wip = int(self.cfg.production.get("max_in_progress", 0))
        formats = self.cfg.production.get("auto_formats")
        active = self._active_count()
        q = ("SELECT * FROM videos WHERE status IN ('planned','in_production','ready') "
             + ("AND id = ? " if only else "") + "ORDER BY publish_at_utc")
        for v in self.con.execute(q, (only,) if only else ()).fetchall():
            st = self.current_stage(v["id"])
            if st is None:
                continue
            stage, status = st["stage"], st["status"]
            if status == "blocked" and for_stage(stage) and st["note"] == "no_agent":
                status = "pending"  # агент з'явився
            if status == "needs_human" and "WAIT_FILE:" in (st["note"] or ""):
                waited = (st["note"].split("WAIT_FILE:", 1)[1].split()[0]).strip()
                wp = self.cfg.root / waited
                if wp.parent.exists() and any(wp.parent.glob(wp.stem + ".*")):  # власник поклав файл (будь-яке розширення)
                    with tx(self.con):
                        events.emit(self.con, "info", "orchestrator", f"отримано {waited} — етап «{stage}» продовжується",
                                    video_id=v["id"])
                    status = "pending"
            if status != "pending":
                continue
            if not manual and v["status"] == "planned":          # новий слот: формат і ліміт одночасних відео
                if formats and v["format"] not in formats:
                    continue
                if wip and active >= wip:
                    continue
                active += 1
            if stage == "approval":
                self._reach_approval(v["id"])
                continue
            if stage == "publish":
                ok, why = self.can_publish(v["id"])
                if not ok:
                    if st["note"] != why:
                        with tx(self.con):
                            self._set_stage(v["id"], stage, "pending", why)
                            events.emit(self.con, "warning", "orchestrator", why, video_id=v["id"])
                    continue
            if for_stage(stage) is None:
                if st["note"] != "no_agent":
                    with tx(self.con):
                        self._set_stage(v["id"], stage, "blocked", "no_agent")
                        events.emit(self.con, "warning", "orchestrator",
                                    f"етап «{stage}»: агент ще не реалізовано — етап очікує", video_id=v["id"])
                continue
            out.append(self._enqueue_stage(v["id"], stage, v["priority"]))
        return out

    def _qc_route(self, vid: str, res: dict) -> None:
        """Цикл виправлень (у межах транзакції): QC не пройдено → найраніший етап із задач отримує зауваження,
        етапи після нього до QC — повтор; після max_rounds або без конкретних задач — людина."""
        rnd, fixes = int(res.get("round", 1)), res.get("fixes") or []
        verdict = "BLOCKED_CRITICAL" if res.get("critical") else res.get("verdict")
        if rnd >= int(self.cfg.qc.get("max_rounds", 3)) or not fixes:
            why = (f"QC {verdict} після {rnd} раунд(ів)" if fixes else f"QC {verdict}, але без конкретних задач")                 + f" — потрібне рішення людини ({res.get('report', '')})"
            if verdict != "BLOCKED_CRITICAL":
                # без переробок: етап QC виконано, звіт і задачі — на погодження фіналу людиною
                self.con.execute("UPDATE fix_tasks SET status='human' WHERE video_id=? AND status='open'", (vid,))
                events.emit(self.con, "warning", "qc",
                            why.replace("потрібне рішення людини", "рішення на погодженні фіналу"), video_id=vid)
                return
            self._set_stage(vid, "qc", "needs_human", why)
            self.con.execute("UPDATE fix_tasks SET status='human' WHERE video_id=? AND status='open'", (vid,))
            events.emit(self.con, "warning", "qc", why, video_id=vid)
            return
        qi = STAGES.index("qc")
        target = min((f["stage"] for f in fixes if f["stage"] in STAGES[:qi]), key=STAGES.index, default="edit")
        for s in STAGES[STAGES.index(target):qi + 1]:
            mine = [f for f in fixes if f["stage"] == s]
            if s == "thumbnail" and not mine and target not in ("script",):
                continue                                         # обкладинку не переробляємо без причини
            if mine:
                note = f"доопрацювати: QC r{rnd} — " + "; ".join(
                    f"{f['scope']}: {f['description']} (приймання: {f['criterion']})" for f in mine)[:1800]
            else:
                note = f"повтор після виправлень QC r{rnd} (етап «{target}»)"
            self._set_stage(vid, s, "pending", note)
        self.con.execute("UPDATE fix_tasks SET status='sent' WHERE video_id=? AND status='open'", (vid,))
        events.emit(self.con, "warning", "qc", f"QC r{rnd}: {verdict} — повернуто на «{target}» ({len(fixes)} задач)",
                    video_id=vid)

    def _reach_approval(self, vid: str) -> None:
        with tx(self.con):
            verdict = self.qc_verdict(vid)
            if "approval" not in self.mode.gates and verdict in QC_PASS:
                self._set_stage(vid, "approval", "done", f"автопогодження: QC {verdict}", approved_by="auto")
                self.con.execute("UPDATE videos SET status='ready', updated_at=? WHERE id=?", (clock.now_iso(), vid))
                events.emit(self.con, "info", "orchestrator", f"автопогодження (QC {verdict})", video_id=vid)
            else:
                self._set_stage(vid, "approval", "awaiting_approval", f"QC: {verdict or 'немає'}")
                events.emit(self.con, "info", "orchestrator", "фінальне відео очікує погодження", video_id=vid)

    def start_stage(self, vid: str, stage: str) -> int:
        """Ручний запуск (режим manual). Попередні етапи мають бути завершені."""
        cur = self.current_stage(vid)
        if cur is None or cur["stage"] != stage:
            raise ValueError(f"зараз активний етап «{cur['stage'] if cur else '—'}», а не «{stage}»")
        if cur["status"] not in ("pending", "blocked", "needs_human", "failed"):
            raise ValueError(f"етап «{stage}» у статусі {cur['status']}")
        if for_stage(stage) is None:
            raise ValueError(f"агент для етапу «{stage}» ще не реалізовано")
        if stage == "publish":                       # ручний старт не обходить QC і погодження
            ok, why = self.can_publish(vid)
            if not ok:
                raise ValueError(why)
        if self.paused():
            raise RuntimeError("студію призупинено (STOP) — спершу resume")
        return self._enqueue_stage(vid, stage, 10)

    def approve(self, vid: str, stage: str, who: str = "owner", force: bool = False) -> None:
        """force — людина вручну перевірила результат етапу, який зупинився (needs_human/failed), напр. через хибну
        перевірку агента, і приймає його без нового прогону сесії. Фіксується в журналі подій."""
        allowed = ("awaiting_approval", "needs_human", "failed") if force else ("awaiting_approval",)
        with tx(self.con):
            row = self.con.execute("SELECT status, note FROM stages WHERE video_id=? AND stage=?", (vid, stage)).fetchone()
            if not row or row["status"] not in allowed:
                raise ValueError(f"{vid}/{stage} не очікує погодження ({row['status'] if row else 'немає'})")
            if row["status"] != "awaiting_approval":
                events.emit(self.con, "warning", "approval",
                            f"«{stage}» прийнято вручну зі статусу {row['status']} ({who}); було: {(row['note'] or '')[:300]}",
                            video_id=vid)
            self._set_stage(vid, stage, "done", None, approved_by=who)
            if stage == "approval":
                self.con.execute("UPDATE videos SET status='ready', updated_at=? WHERE id=?", (clock.now_iso(), vid))
            events.emit(self.con, "info", "approval", f"«{stage}» погоджено ({who})", video_id=vid)

    def reject(self, vid: str, stage: str, note: str) -> None:
        """Повернути етап на доопрацювання з коментарем (новий прогін агента, нова ревізія)."""
        with tx(self.con):
            self._set_stage(vid, stage, "pending", f"доопрацювати: {note}")
            events.emit(self.con, "warning", "approval", f"«{stage}» повернуто: {note}", video_id=vid)

    SWAP_TABLES = (("stages", "video_id"), ("jobs", "video_id"), ("events", "video_id"), ("qc_reports", "video_id"),
                   ("fix_tasks", "video_id"), ("uploads", "video_id"), ("topics", "assigned_video"))

    def swap_slots(self, a: str, b: str) -> None:
        """Поміняти відео двох слотів календаря одного формату (напр. готове відео — на ближчий слот): уся історія
        (етапи, задачі, QC, події, тема, тека епізоду) переходить разом із відео; дедлайни етапів — за новим часом."""
        va = self.con.execute("SELECT * FROM videos WHERE id=?", (a,)).fetchone()
        vb = self.con.execute("SELECT * FROM videos WHERE id=?", (b,)).fetchone()
        if not va or not vb:
            raise ValueError(f"немає слоту {a if not va else b}")
        if va["format"] != vb["format"]:
            raise ValueError("слоти різних форматів")
        if self.con.execute("SELECT 1 FROM jobs WHERE video_id IN (?,?) AND status='running'", (a, b)).fetchone():
            raise ValueError("для одного зі слотів виконується задача — дочекайтесь завершення")
        if any(v["status"] in ("published", "late") for v in (va, vb)):
            raise ValueError("опубліковані/прострочені слоти не переносяться")
        with tx(self.con):
            self.con.execute("PRAGMA defer_foreign_keys = ON")
            for table, col in self.SWAP_TABLES:
                self.con.execute(f"UPDATE {table} SET {col}='__swap__' WHERE {col}=?", (a,))
                self.con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (a, b))
                self.con.execute(f"UPDATE {table} SET {col}=? WHERE {col}='__swap__'", (b,))
            fields = ("topic", "title", "priority", "episode_dir", "source_video_id", "status")
            now = clock.now_iso()
            for dst, src in ((a, vb), (b, va)):
                self.con.execute(f"UPDATE videos SET {', '.join(f + '=?' for f in fields)}, updated_at=? WHERE id=?",
                                 (*[src[f] for f in fields], now, dst))
            for vid, v in ((a, va), (b, vb)):          # дедлайни етапів — від часу слоту, а не від відео
                local = clock.parse(v["publish_at_utc"]).astimezone(self.cfg.tz).replace(tzinfo=None)
                for st in STAGES:
                    dl = scheduler.stage_deadline(self.cfg, v["format"], st, local)
                    self.con.execute("UPDATE stages SET deadline_utc=? WHERE video_id=? AND stage=?",
                                     (clock.iso(dl) if dl else None, vid, st))
            events.emit(self.con, "warning", "orchestrator", f"слоти поміняно: {a} ⇄ {b}", video_id=a)
            events.emit(self.con, "warning", "orchestrator", f"слоти поміняно: {b} ⇄ {a}", video_id=b)

    def set_topic(self, vid: str, topic: str) -> None:
        with tx(self.con):
            self.con.execute("UPDATE videos SET topic=?, updated_at=? WHERE id=?", (topic, clock.now_iso(), vid))

    # ---------- воркер ----------
    def _free_gb(self) -> float:
        return shutil.disk_usage(self.cfg.root).free / 1e9

    def work_once(self) -> dict | None:
        """Взяти й виконати одну задачу. None — роботи немає (або STOP)."""
        if self.paused():
            return None
        job = queue.claim(self.con, self.worker_id, kinds=set(REGISTRY), lease_seconds=self.cfg.lease_seconds,
                          exclusive=self.cfg.exclusive)
        if job is None:
            return None
        cls = REGISTRY[job["kind"]]
        jid, vid, stage = job["id"], job["video_id"], job["stage"]

        if HEAVY & set(json.loads(job["resources"])) and self._free_gb() < self.cfg.min_free_disk_gb:
            # стан середовища, а не збій задачі: відкласти без витрати спроби (як ліміт Claude), людині — попередження
            msg = f"мало місця на диску ({self._free_gb():.1f} ГБ < {self.cfg.min_free_disk_gb} ГБ) — звільніть місце"
            until = clock.iso(clock.now() + timedelta(minutes=30))
            queue.defer(self.con, jid, self.worker_id, until, msg)
            events.emit(self.con, "warning", "orchestrator", f"«{stage}» відкладено до {until}: {msg}",
                        video_id=vid, job_id=jid)
            return {"job": jid, "status": "deferred_disk", "until": until, "error": msg}

        stop_hb = threading.Event()
        hb_con = db.connect(self.cfg.db_path)

        def hb_loop():
            while not stop_hb.wait(max(5, self.cfg.lease_seconds // 3)):
                queue.heartbeat(hb_con, jid, self.worker_id, self.cfg.lease_seconds)

        t = threading.Thread(target=hb_loop, daemon=True)
        t.start()

        def log(level: str, msg: str) -> None:
            events.emit(self.con, level, cls.name, msg, video_id=vid, job_id=jid)

        ctx = Context(con=self.con, cfg=self.cfg, job_id=jid, video_id=vid, stage=stage,
                      heartbeat=lambda: queue.heartbeat(self.con, jid, self.worker_id, self.cfg.lease_seconds),
                      log=log, should_stop=self.paused)
        try:
            events.emit(self.con, "info", cls.name, "старт", video_id=vid, job_id=jid)
            res: AgentResult = cls().run(ctx, json.loads(job["payload"]))
            if not isinstance(res, AgentResult) or not res.ok:
                raise BlockedError(getattr(res, "summary", "") or "агент повернув неуспішний результат")
            payload = {"summary": res.summary, "artifacts": res.artifacts, **res.data}
            queue.complete(self.con, jid, self.worker_id, payload)
            if stage and vid:
                with tx(self.con):
                    gated = stage in self.mode.gates
                    self._set_stage(vid, stage, "awaiting_approval" if gated else "done", res.summary[:500],
                                    result=json.dumps(payload, ensure_ascii=False))
                    if stage == "qc" and (payload.get("critical") or payload.get("verdict") not in QC_PASS):
                        self._qc_route(vid, payload)
                    if stage == "publish":
                        self.con.execute("UPDATE videos SET status='published', updated_at=? WHERE id=?",
                                         (clock.now_iso(), vid))
                    events.emit(self.con, "info", cls.name,
                                f"етап «{stage}» виконано" + (" — очікує погодження" if gated else ""),
                                video_id=vid, job_id=jid)
            return {"job": jid, "status": "done", "summary": res.summary}
        except LimitError as e:
            until = e.until or (clock.now() + timedelta(minutes=30))
            queue.defer(self.con, jid, self.worker_id, clock.iso(until), str(e))
            return {"job": jid, "status": "deferred_limit", "until": clock.iso(until), "error": str(e)}
        except RetryableError as e:
            st = queue.fail(self.con, jid, self.worker_id, f"{type(e).__name__}: {e}", retry=True)
            return {"job": jid, "status": st, "error": str(e)}
        except BlockedError as e:
            queue.fail(self.con, jid, self.worker_id, f"потрібна людина: {e}", retry=False)
            return {"job": jid, "status": "dead", "error": str(e)}
        except Exception as e:  # noqa: BLE001 — невідома помилка: повторюємо, трасу — в журнал
            tb = traceback.format_exc(limit=8)
            st = queue.fail(self.con, jid, self.worker_id, f"{type(e).__name__}: {e}\n{tb}", retry=True)
            return {"job": jid, "status": st, "error": str(e)}
        finally:
            stop_hb.set()
            t.join(timeout=2)
            hb_con.close()

    def run_forever(self, interval: float = 30.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        events.emit(self.con, "info", "orchestrator", f"старт оркестратора {self.worker_id}, режим {self.mode_name}")
        while not stop.is_set():
            try:
                self.tick()
                while not stop.is_set() and self.work_once():
                    pass
            except Exception as e:  # noqa: BLE001 — цикл не повинен падати мовчки
                events.emit(self.con, "error", "orchestrator", f"помилка циклу: {e}",
                            data={"trace": traceback.format_exc(limit=8)})
            stop.wait(interval)

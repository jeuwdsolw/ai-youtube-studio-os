"""CLI Studio OS.

    tools/studio-venv/Scripts/python.exe -m studio_os <команда>

status · calendar · plan · tick · work [--once] · run · pause/resume · mode <manual|semi|auto>
start/approve/reject <video> <stage> · topic <video> <текст> · jobs · retry/cancel <job> · events · backup
cleanup-frames [--apply] · qc <тека епізоду> [--format short|long] [--llm]
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config
from .core import clock, db, events, queue
from .orchestrator import Orchestrator

STAGE_ABBR = {"research": "Rs", "planning": "Pl", "script": "Sc", "voice": "Vo", "storyboard": "Sb",
              "animation": "An", "edit": "Ed", "thumbnail": "Th", "qc": "QC", "approval": "Ap", "publish": "Pb"}
STATUS_MARK = {"pending": "·", "queued": "q", "running": "▶", "awaiting_approval": "?", "done": "✓",
               "failed": "✗", "blocked": "■", "needs_human": "!", "skipped": "-"}


def _orch(args) -> Orchestrator:
    cfg = config.load()
    events.setup_file_log(cfg.log_dir)
    con = db.open_db(cfg.db_path)
    return Orchestrator(con, cfg)


def cmd_status(o: Orchestrator, a) -> None:
    con = o.con
    print(f"Режим: {o.mode_name}   STOP: {'ТАК' if o.paused() else 'ні'}   БД: {o.cfg.db_path}")
    rows = con.execute("SELECT format, status, COUNT(*) n FROM videos GROUP BY format, status").fetchall()
    for r in rows:
        print(f"  {r['format']:5} {r['status']:13} {r['n']}")
    aw = con.execute("SELECT COUNT(*) FROM stages WHERE status='awaiting_approval'").fetchone()[0]
    nh = con.execute("SELECT COUNT(*) FROM stages WHERE status='needs_human'").fetchone()[0]
    print(f"Очікують погодження: {aw}   Потрібна людина: {nh}")
    jq = con.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    print("Черга: " + (", ".join(f"{r['status']} {r['n']}" for r in jq) or "порожня"))
    print("Найближчі публікації:")
    for v in con.execute("SELECT * FROM videos WHERE publish_at_utc >= ? AND status NOT IN ('cancelled') "
                         "ORDER BY publish_at_utc LIMIT 5", (clock.now_iso(),)):
        cur = o.current_stage(v["id"])
        print(f"  {v['publish_local']} {v['format']:5} {v['id']}  {v['topic'] or '(тема не обрана)'}"
              f"  → {cur['stage'] + ':' + cur['status'] if cur else 'усе готово'}")


def cmd_calendar(o: Orchestrator, a) -> None:
    hdr = " ".join(STAGE_ABBR.values())
    print(f"{'дата/час (' + o.cfg.tz.key + ')':24} {'тип':5} {'id':15} {hdr}  тема")
    rows = o.con.execute("SELECT * FROM videos WHERE status != 'cancelled' ORDER BY publish_at_utc").fetchall()
    for v in rows[: a.limit]:
        st = {s["stage"]: s["status"] for s in o._stages(v["id"])}
        marks = "  ".join(STATUS_MARK.get(st.get(k, ""), " ") for k in STAGE_ABBR)
        print(f"{v['publish_local']:24} {v['format']:5} {v['id']:15} {marks}   {v['topic'] or ''}")
    print("\n· очікує  q у черзі  ▶ виконується  ? погодження  ✓ готово  ✗ помилка  ■ заблоковано  ! потрібна людина")


def cmd_jobs(o: Orchestrator, a) -> None:
    q = "SELECT * FROM jobs " + ("WHERE status = ? " if a.status else "") + "ORDER BY id DESC LIMIT ?"
    for j in o.con.execute(q, ((a.status,) if a.status else ()) + (a.limit,)):
        err = (j["last_error"] or "").splitlines()[0][:80] if j["last_error"] else ""
        print(f"#{j['id']:<5} {j['status']:9} {j['kind']:30} {j['video_id'] or '':15} "
              f"спроб {j['attempts']}/{j['max_attempts']} {err}")


def cmd_events(o: Orchestrator, a) -> None:
    for e in reversed(events.recent(o.con, a.limit, a.level)):
        ctx = " ".join(x for x in (e["video_id"], e["job_id"] and f"#{e['job_id']}") if x)
        print(f"{e['ts']} {e['level']:8} {e['source']:22} {ctx:22} {e['message']}")


def cmd_cleanup(o: Orchestrator, a) -> None:
    key = f"cleanup_frames:{clock.now_iso()}:{'apply' if a.apply else 'dry'}"
    jid = queue.enqueue(o.con, "maintenance.cleanup_frames", key, {"dry_run": not a.apply}, priority=0, max_attempts=1)
    o.work_once()
    j = o.con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if j["status"] != "done":
        print(f"Задача #{jid}: {j['status']} — {j['last_error']}")
        sys.exit(1)
    res = json.loads(j["result"])
    for d in res["dirs"]:
        print(f"{d['dir']:45} {d['pngs']:5} PNG {d['mb']:8.1f} МБ  відео: {d['video_frames']} кадрів  → {d['decision']}")
    print(res["summary"])


def cmd_youtube(o, a) -> None:
    """youtube-auth: вхід власниці в браузері → токен + канал у data/youtube/token.json (значення не друкуються)."""
    from .adapters.youtube_upload import YouTubeAuth, YouTubeUploader
    auth = YouTubeAuth(o.cfg.root / "data" / "youtube")
    if a.cmd == "youtube-auth":
        auth.authorize()
    ch = YouTubeUploader(auth).my_channel()
    if a.cmd == "youtube-auth":
        auth.save_channel(ch)
    print(f"Канал: {ch['title']} ({ch['id']}) — публікації йтимуть сюди.")


def cmd_qc(o: Orchestrator, a) -> None:
    from . import qc
    ep = o.cfg.root / "episodes" / a.episode
    if not ep.is_dir():
        raise ValueError(f"немає теки {ep}")
    fmt = a.format or ("long" if a.episode.startswith("long-") else "short")
    rep = qc.run(o.cfg.root, ep, fmt, o.cfg, llm=a.llm)
    out = o.cfg.root / "data" / "qc" / a.episode
    out.mkdir(parents=True, exist_ok=True)
    (out / "qc.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    md = qc.modules.to_md(a.episode, rep, 0)
    (out / "qc.md").write_text(md, encoding="utf-8")
    print(md[:6000])
    print(f"Звіт: {out / 'qc.md'}")


def main(argv: list[str] | None = None) -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    p = argparse.ArgumentParser(prog="studio_os", description="Studio OS — оркестратор студії")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("calendar"); c.add_argument("--limit", type=int, default=40)
    c = sub.add_parser("plan"); c.add_argument("--weeks", type=int)
    sub.add_parser("tick")
    c = sub.add_parser("work"); c.add_argument("--once", action="store_true")
    c = sub.add_parser("run"); c.add_argument("--interval", type=float, default=30)
    c = sub.add_parser("pause"); c.add_argument("reason", nargs="?", default="")
    sub.add_parser("resume")
    c = sub.add_parser("mode"); c.add_argument("name", choices=config.MODES)
    for name in ("start", "approve"):
        c = sub.add_parser(name); c.add_argument("video"); c.add_argument("stage", choices=config.STAGES)
    c.add_argument("--force", action="store_true", help="прийняти етап needs_human/failed після ручної перевірки")
    c = sub.add_parser("reject"); c.add_argument("video"); c.add_argument("stage", choices=config.STAGES); c.add_argument("note")
    c = sub.add_parser("topic"); c.add_argument("video"); c.add_argument("text")
    c = sub.add_parser("swap", help="поміняти відео двох слотів одного формату (з усією історією)")
    c.add_argument("a"); c.add_argument("b")
    sub.add_parser("youtube-auth", help="підключити канал YouTube (вхід власниці в браузері, одноразово)")
    sub.add_parser("youtube-channel", help="показати підключений канал (перевірка токена)")
    c = sub.add_parser("jobs"); c.add_argument("--status"); c.add_argument("--limit", type=int, default=30)
    for name in ("retry", "cancel"):
        c = sub.add_parser(name); c.add_argument("job", type=int)
    c = sub.add_parser("events"); c.add_argument("--limit", type=int, default=40)
    c.add_argument("--level", default="info", choices=["debug", "info", "warning", "error", "critical"])
    sub.add_parser("backup")
    c = sub.add_parser("cleanup-frames"); c.add_argument("--apply", action="store_true", help="справді видалити (інакше dry run)")
    c = sub.add_parser("qc", help="QC будь-якого епізоду поза конвеєром; звіт — data/qc/<епізод>/ (теки епізоду не змінюються)")
    c.add_argument("episode"); c.add_argument("--format", choices=["short", "long"])
    c.add_argument("--llm", action="store_true", help="з рецензією Claude (витрачає сесію підписки)")
    a = p.parse_args(argv)

    o = _orch(a)
    try:
        match a.cmd:
            case "status": cmd_status(o, a)
            case "calendar": cmd_calendar(o, a)
            case "plan":
                from .core import scheduler
                ids = scheduler.plan(o.con, o.cfg, weeks=a.weeks)
                print(f"Нових слотів: {len(ids)}")
            case "tick": print(json.dumps(o.tick(), ensure_ascii=False, indent=1))
            case "work":
                while True:
                    r = o.work_once()
                    print(r or "Роботи немає")
                    if a.once or not r:
                        break
            case "run": o.run_forever(a.interval)
            case "pause": o.pause(a.reason); print("STOP: нові задачі не стартують.")
            case "resume": o.resume(); print("Відновлено.")
            case "mode": o.set_mode(a.name); print(f"Режим: {a.name}")
            case "start": print(f"Задача #{o.start_stage(a.video, a.stage)}")
            case "approve": o.approve(a.video, a.stage, force=a.force); print("Погоджено.")
            case "reject": o.reject(a.video, a.stage, a.note); print("Повернуто на доопрацювання.")
            case "topic": o.set_topic(a.video, a.text); print("Тему збережено.")
            case "jobs": cmd_jobs(o, a)
            case "retry": print("OK" if queue.retry_dead(o.con, a.job) else "Задачу не знайдено або вона активна")
            case "cancel": print("OK" if queue.cancel(o.con, a.job) else "Задачу не знайдено")
            case "events": cmd_events(o, a)
            case "backup": print(db.backup(o.con, o.cfg.backup_dir, o.cfg.backups_keep))
            case "cleanup-frames": cmd_cleanup(o, a)
            case "qc": cmd_qc(o, a)
            case "swap": o.swap_slots(a.a, a.b); print(f"Слоти поміняно: {a.a} ⇄ {a.b}")
            case "youtube-auth" | "youtube-channel": cmd_youtube(o, a)
    except (ValueError, RuntimeError, KeyError) as e:
        print(f"Помилка: {e}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("Зупинено.")


if __name__ == "__main__":
    main()

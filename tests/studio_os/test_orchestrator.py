import json
import shutil
import subprocess

import pytest

from studio_os.agents import maintenance
from studio_os.agents.base import Agent, AgentResult, BlockedError, RetryableError, register
from studio_os.core import queue, scheduler
from studio_os.orchestrator import Orchestrator

VID = "S20261123-1800"


def _stage(con, stage, vid=VID):
    return con.execute("SELECT * FROM stages WHERE video_id=? AND stage=?", (vid, stage)).fetchone()


def _fake(stage_name, behaviour="ok", resources=()):
    @register
    class Fake(Agent):
        name = f"fake.{stage_name}"
        stage = stage_name
        max_attempts = 2

        def run(self, ctx, payload):
            if behaviour == "retry":
                raise RetryableError("тимчасово")
            if behaviour == "blocked":
                raise BlockedError("немає ключа")
            if stage_name == "qc":
                return AgentResult(summary="qc", data={"verdict": "APPROVED", "score": 93, "critical": []})
            return AgentResult(summary=f"{stage_name} ok", artifacts=[f"x/{stage_name}.md"])

    Fake.resources = tuple(resources)
    return Fake


@pytest.fixture
def orch(con, cfg, fclock):
    o = Orchestrator(con, cfg, "test-worker")
    scheduler.plan(con, cfg, weeks=1)
    return o


def test_manual_mode_does_not_autostart(orch, con, registry):
    _fake("research")
    assert orch.mode_name == "manual"
    assert orch.advance() == []
    jid = orch.start_stage(VID, "research")
    assert orch.work_once()["status"] == "done"
    assert _stage(con, "research")["status"] == "awaiting_approval"   # manual: кожен етап погоджується
    with pytest.raises(ValueError):
        orch.start_stage(VID, "planning")                              # спершу погодження research
    orch.approve(VID, "research")
    assert _stage(con, "research")["approved_by"] == "owner"
    assert jid


def test_missing_agent_is_reported_not_skipped(orch, con, registry):
    orch.set_mode("semi")
    orch.advance()
    st = _stage(con, "research")
    assert st["status"] == "blocked" and st["note"] == "no_agent"
    n_events = con.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%агент ще не реалізовано%'").fetchone()[0]
    orch.advance()
    assert con.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%агент ще не реалізовано%'").fetchone()[0] == n_events
    _fake("research")                                                  # агент з'явився → етап піде в чергу
    assert orch.advance()
    assert _stage(con, "research")["status"] == "queued"


def test_semi_mode_gates_only_planning_script_approval(orch, con, registry):
    for s in ("research", "planning"):
        _fake(s)
    orch.set_mode("semi")
    orch.advance(only=VID)
    orch.work_once()
    assert _stage(con, "research")["status"] == "done"                 # без погодження в semi
    orch.advance(only=VID)
    orch.work_once()
    assert _stage(con, "planning")["status"] == "awaiting_approval"   # теми погоджує людина


def test_publish_blocked_without_qc_and_approval(orch, con, registry):
    orch.set_mode("auto")
    for s in ("research", "planning", "script", "voice", "storyboard", "animation", "edit", "thumbnail"):
        _fake(s)
    _fake("publish")
    for _ in range(8):
        orch.advance(only=VID)
        orch.work_once()
    assert orch.current_stage(VID)["stage"] == "qc"
    ok, why = orch.can_publish(VID)
    assert not ok and "QC" in why
    _fake("qc")
    orch.advance(only=VID); orch.work_once()                           # QC APPROVED
    orch.advance(only=VID)                                             # approval: auto → done, але auto_publish=false
    assert _stage(con, "approval")["approved_by"] == "auto"
    orch.advance(only=VID)
    assert _stage(con, "publish")["status"] == "pending"
    assert "автопублікацію не дозволено" in _stage(con, "publish")["note"]
    assert not con.execute("SELECT 1 FROM jobs WHERE stage='publish'").fetchone()


def test_critical_qc_issue_blocks_even_with_high_score(orch, con):
    con.execute("UPDATE stages SET status='done', result=? WHERE video_id=? AND stage='qc'",
                (json.dumps({"verdict": "APPROVED", "score": 97, "critical": ["фотореалістичний кадр"]}), VID))
    assert orch.qc_verdict(VID) == "BLOCKED_CRITICAL"
    assert not orch.can_publish(VID)[0]


def test_stop_switch_blocks_new_work(orch, con, registry):
    _fake("research")
    orch.start_stage(VID, "research")
    orch.pause("тест")
    assert orch.stop_file.exists()
    assert orch.work_once() is None
    with pytest.raises(RuntimeError):
        orch.start_stage("S20261124-1800", "research")
    orch.resume()
    assert orch.work_once()["status"] == "done"


def test_retry_then_needs_human(orch, con, registry, fclock):
    _fake("research", behaviour="retry")
    orch.start_stage(VID, "research")
    assert orch.work_once()["status"] == "queued"
    fclock.advance(seconds=31)
    assert orch.work_once()["status"] == "dead"
    assert _stage(con, "research")["status"] == "needs_human"


def test_blocked_error_goes_to_human_immediately(orch, con, registry):
    _fake("research", behaviour="blocked")
    orch.start_stage(VID, "research")
    assert orch.work_once()["status"] == "dead"
    assert "потрібна людина" in _stage(con, "research")["note"]


def test_force_approve_accepts_checked_needs_human_stage(orch, con, registry):
    _fake("research", behaviour="blocked")
    orch.start_stage(VID, "research")
    orch.work_once()
    with pytest.raises(ValueError):
        orch.approve(VID, "research")                       # без force — лише awaiting_approval
    orch.approve(VID, "research", who="owner", force=True)  # людина перевірила результат вручну
    assert _stage(con, "research")["status"] == "done"
    ev = con.execute("SELECT message FROM events WHERE message LIKE '%прийнято вручну%'").fetchone()
    assert ev and "needs_human" in ev["message"]
    assert orch.current_stage(VID)["stage"] == "planning"


def test_low_disk_defers_without_spending_attempts(orch, con, cfg, registry, monkeypatch):
    _fake("edit", resources=("remotion",))
    for s in ("research", "planning", "script", "voice", "storyboard", "animation"):
        con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage=?", (VID, s))
    con.commit()
    orch.start_stage(VID, "edit")
    monkeypatch.setattr(orch, "_free_gb", lambda: 1.0)
    for _ in range(3):                                   # більше, ніж max_attempts — і все одно не dead
        r = orch.work_once()
        assert r is None or r["status"] == "deferred_disk"
        con.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00Z' WHERE stage='edit'"); con.commit()
    job = con.execute("SELECT status, attempts FROM jobs WHERE stage='edit'").fetchone()
    assert job["status"] == "queued" and job["attempts"] == 0
    monkeypatch.setattr(orch, "_free_gb", lambda: 500.0)
    assert orch.work_once()["status"] == "done"


def test_exhausted_claude_budget_defers_to_next_day(orch, con, cfg, registry, fclock):
    from studio_os.core import budget
    from studio_os.errors import LimitError

    @register
    class NeedsClaude(Agent):
        name = "fake.script.budget"
        stage = "research"
        max_attempts = 1

        def run(self, ctx, payload):
            budget.require(ctx.con, ctx.cfg, "claude_sessions")
            return AgentResult(summary="ok")

    budget.spend(con, cfg, "claude_sessions", cfg.budgets["claude_sessions"].limit)
    orch.start_stage(VID, "research")
    r = orch.work_once()
    assert r["status"] == "deferred_limit" and r["until"] == "2026-11-23T22:05:00Z"   # 00:05 Київ наступного дня
    job = con.execute("SELECT status, attempts FROM jobs").fetchone()
    assert job["status"] == "queued" and job["attempts"] == 0                         # не «потрібна людина»


def test_restart_recovers_running_job(orch, con, cfg, registry, fclock):
    _fake("research")
    orch.start_stage(VID, "research")
    queue.claim(con, "crashed-process", lease_seconds=cfg.lease_seconds, exclusive=cfg.exclusive)
    fclock.advance(seconds=cfg.lease_seconds + 1)
    fresh = Orchestrator(con, cfg, "after-restart")
    assert fresh.tick()["recovered"]
    fclock.advance(seconds=31)
    assert fresh.work_once()["status"] == "done"


def test_daily_backup_once_per_day(orch, cfg, fclock):
    assert orch.tick()["backup"]
    assert orch.tick()["backup"] is None
    fclock.advance(days=1)
    assert orch.tick()["backup"]


@pytest.mark.skipif(not shutil.which("ffmpeg") or not maintenance.find_ffprobe(), reason="потрібен ffmpeg")
def test_cleanup_frames_only_when_encoded(cfg, con, fclock):
    root = cfg.root
    ep = root / "episodes" / "episode-900"
    (ep / "renders" / "shotA").mkdir(parents=True)
    (ep / "renders" / "shotB").mkdir(parents=True)
    (ep / "pipeline.json").write_text(json.dumps({"public_dir": "video/public/ep900"}), encoding="utf-8")
    pub = root / "video" / "public" / "ep900"
    pub.mkdir(parents=True)
    for shot in ("shotA", "shotB"):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=navy:s=32x32:d=0.2:r=30",
                        str(ep / "renders" / shot / "f_%04d.png")], check=True)
    (ep / "renders" / "shotA" / "check.png").write_bytes(b"keep")
    subprocess.run(["ffmpeg", "-v", "error", "-framerate", "30", "-i", str(ep / "renders" / "shotA" / "f_%04d.png"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(pub / "shotA.mp4")], check=True)
    o = Orchestrator(con, cfg, "w")
    queue.enqueue(con, "maintenance.cleanup_frames", "dry", {"dry_run": True})
    assert o.work_once()["status"] == "done"
    assert len(list((ep / "renders" / "shotA").glob("f_*.png"))) == 6          # dry run нічого не видаляє
    queue.enqueue(con, "maintenance.cleanup_frames", "apply", {"dry_run": False})
    assert o.work_once()["status"] == "done"
    assert not list((ep / "renders" / "shotA").glob("f_*.png"))
    assert (ep / "renders" / "shotA" / "check.png").exists()                    # інші файли лишились
    assert len(list((ep / "renders" / "shotB").glob("f_*.png"))) == 6          # без відео — не чіпаємо


def test_conveyor_starts_one_short_at_a_time_and_gates_only_final(orch, con, cfg, registry):
    for s in ("research", "planning", "script", "voice", "storyboard", "animation", "edit", "thumbnail", "qc"):
        _fake(s)
    orch.set_mode("conveyor")
    cfg.production.update(max_in_progress=1, auto_formats=["short"])
    orch.advance()
    started = con.execute("SELECT DISTINCT video_id FROM jobs").fetchall()
    assert len(started) == 1 and started[0][0].startswith("S")           # один Shorts, Long не стартує
    first = started[0][0]
    for _ in range(12):
        orch.advance()
        orch.work_once()
    assert _stage(con, "approval", first)["status"] == "awaiting_approval"    # фінал — лише людина
    assert len(con.execute("SELECT DISTINCT video_id FROM jobs").fetchall()) >= 2   # наступний Shorts пішов
    assert not con.execute("SELECT 1 FROM jobs j JOIN videos v ON v.id=j.video_id WHERE v.format='long'").fetchone()

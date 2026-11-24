import json
import shutil
import subprocess

import pytest

from conftest import STUDIO
from studio_os import qc as qc_pkg
from studio_os.agents import voice
from studio_os.core import scheduler
from studio_os.orchestrator import Orchestrator
from studio_os.qc import modules, review

VID = "S20261123-1800"
FFMPEG = shutil.which("ffmpeg")


def _iss(module, sev, stage="edit"):
    return modules.issue(module, sev, f"{module} {sev}", stage, "критерій")


# ---------- агрегація ----------
def test_aggregate_weights_verdicts_and_missing_modules():
    rep = modules.aggregate({"issues": [], "measures": {}, "not_checked": []}, None)
    assert rep["modules"]["story"]["score"] is None and rep["modules"]["style"]["score"] is None   # без рецензента — немає даних
    assert rep["score"] == 100 and rep["verdict"] == "APPROVED"
    rep = modules.aggregate({"issues": [_iss("audio", "major"), _iss("editing", "minor")], "measures": {}, "not_checked": []},
                            {"scores": {"story": 70, "dialogue": 90, "retention": 80, "style": 60, "youtube": 100},
                             "issues": [_iss("story", "major", "script")]})
    m = rep["modules"]
    assert m["audio"]["score"] == 75 and m["editing"]["score"] == 92
    assert m["story"]["score"] == round((75 + 70) / 2)                 # авто-штраф і бал рецензента — середнє
    assert rep["verdict"] in ("APPROVED_WITH_WARNINGS", "NEEDS_REVISION")
    assert {f["stage"] for f in rep["fixes"]} == {"edit", "script"}   # major → задачі; minor — ні


def test_critical_blocks_regardless_of_score():
    rep = modules.aggregate({"issues": [_iss("style", "critical", "animation")], "measures": {}, "not_checked": []},
                            {"scores": {m: 100 for m in review.MODS}, "issues": []})
    assert rep["score"] >= 90 and rep["verdict"] == "BLOCKED_CRITICAL" and rep["critical"]
    assert modules.verdict_for(85) == "APPROVED_WITH_WARNINGS" and modules.verdict_for(65) == "NEEDS_REVISION"
    assert modules.verdict_for(40) == "REJECTED"


def test_youtube_meta_parses_studio_format():
    meta = modules.youtube_meta(STUDIO / "episodes" / "episode-017")
    assert len(meta["titles"]) == 3 and meta["description"] and meta["tags"] and meta["materials"]


# ---------- детерміновані перевірки на справжньому відео ----------
@pytest.fixture
def qc_studio(cfg):
    r = cfg.root
    for rel in ("tests/check_video.py", "tests/check_storyboard.py", "episodes/episode-017/youtube.md"):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(STUDIO / rel, r / rel)
    return r


def _make_video(path, black=(5.0, 6.5), dur=12):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=540x960:r=30:d={dur}",
                    "-f", "lavfi", "-i", f"sine=f=330:d={dur}",
                    "-vf", f"scale=1080:1920,drawbox=c=black:t=fill:enable='between(t,{black[0]},{black[1]})'",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True)


@pytest.mark.skipif(not FFMPEG, reason="потрібен ffmpeg")
def test_auto_checks_find_black_frame_weak_hook_and_missing_files(qc_studio, cfg):
    ep = qc_studio / "episodes" / "episode-900"
    _make_video(ep / "renders" / "final_master.mp4")
    (ep / "audio").mkdir(parents=True)
    (ep / "audio" / "voice-lines.json").write_text(json.dumps([
        {"id": "NR-01", "start": 4.2, "end": 7.0, "text": "Перша репліка надто пізно для Shorts."},
        {"id": "NR-02", "start": 7.3, "end": 9.0, "text": "Друга."}], ensure_ascii=False), encoding="utf-8")
    auto = modules.run_auto(qc_studio, ep, "short", cfg)
    msgs = [(i["module"], i["severity"], i["msg"]) for i in auto["issues"]]
    assert any(m == "editing" and s == "critical" and "чорний екран" in t for m, s, t in msgs), msgs
    assert any(m == "retention" and s == "critical" and "хук" in t for m, s, t in msgs)
    assert any(m == "youtube" and "youtube.md" in t for m, s, t in msgs)
    assert any(m == "youtube" and "cover.jpg" in t for m, s, t in msgs)
    assert any(m == "editing" and "SRT" in t for m, s, t in msgs)
    assert auto["measures"]["video"]["size"] == [1080, 1920]
    rep = modules.aggregate(auto, None)
    assert rep["verdict"] == "BLOCKED_CRITICAL"
    assert "QC" in modules.to_md("episode-900", rep, 1)


def test_missing_video_is_critical(qc_studio, cfg):
    ep = qc_studio / "episodes" / "episode-901"
    ep.mkdir(parents=True)
    auto = modules.run_auto(qc_studio, ep, "short", cfg)
    assert auto["issues"][0]["severity"] == "critical" and "final_master" in auto["issues"][0]["msg"]


# ---------- рецензент ----------
def test_review_uses_read_only_claude_and_filters_issues(qc_studio, cfg, monkeypatch, tmp_path):
    d = tmp_path / "fake_review"
    d.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(d))
    (d / "resp-001.json").write_text(json.dumps({"structured_output": {
        "summary": "ок", "scores": {"story": 80, "dialogue": 90, "retention": 85, "style": 95, "youtube": 100},
        "issues": [{"module": "story", "severity": "major", "msg": "незрозуміло, звідки ключ", "stage": "script",
                    "scope": "S02", "criterion": "ключ показано раніше"},
                   {"module": "вигадане", "severity": "major", "msg": "x", "stage": "script", "scope": "", "criterion": ""}]}},
        ensure_ascii=False), encoding="utf-8")
    ep = qc_studio / "episodes" / "episode-902"
    ep.mkdir(parents=True)
    (ep / "script.md").write_text("# Т", encoding="utf-8")
    out = review.review(qc_studio, ep, "short", cfg, {"issues": [], "frames_dir": None})
    assert [i["module"] for i in out["issues"]] == ["story"]
    args = json.loads((d / "args-001.json").read_text(encoding="utf-8"))
    assert args[args.index("--tools") + 1] == "Read" and "Write" not in " ".join(args)


# ---------- агент QC + цикл виправлень ----------
@pytest.fixture
def orch(con, cfg, fclock, qc_studio):
    o = Orchestrator(con, cfg, "w")
    scheduler.plan(con, cfg, weeks=1)
    con.execute("UPDATE videos SET episode_dir='episodes/episode-777', topic='т' WHERE id=?", (VID,))
    (qc_studio / "episodes/episode-777").mkdir(parents=True)
    for s in ("research", "planning", "script", "voice", "storyboard", "animation", "edit", "thumbnail"):
        con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage=?", (VID, s))
    cfg.qc["llm_review"] = False
    return o


def _report(verdict, fixes=(), critical=()):
    return {"score": {"APPROVED": 95, "NEEDS_REVISION": 70}.get(verdict, 50), "verdict": verdict, "critical": list(critical),
            "modules": {}, "fixes": list(fixes), "not_checked": [], "measures": {}, "review_summary": ""}


def _st(con, stage):
    return con.execute("SELECT * FROM stages WHERE video_id=? AND stage=?", (VID, stage)).fetchone()


def test_qc_fail_routes_to_fix_stage_then_passes(orch, con, monkeypatch):
    orch.cfg.qc["max_rounds"] = 3                            # цикл переробок увімкнено
    fix = {"stage": "animation", "scope": "B03", "description": "[D Animation] статика", "criterion": "рух", "severity": "major"}
    reports = iter([_report("NEEDS_REVISION", [fix]), _report("APPROVED")])
    monkeypatch.setattr(qc_pkg, "run", lambda *a, **k: next(reports))
    orch.set_mode("semi")
    orch.advance(only=VID)
    assert orch.work_once()["status"] == "done"
    assert "доопрацювати: QC r1" in _st(con, "animation")["note"] and "B03" in _st(con, "animation")["note"]
    assert _st(con, "animation")["status"] == "pending" and _st(con, "edit")["status"] == "pending"
    assert _st(con, "thumbnail")["status"] == "done"                       # обкладинку без причини не переробляємо
    assert orch.current_stage(VID)["stage"] == "animation"
    assert con.execute("SELECT status FROM fix_tasks").fetchone()[0] == "sent"
    assert (orch.cfg.root / "episodes/episode-777/qc/qc-r1.md").exists()
    for s in ("animation", "edit"):                                          # виконавці виправили (імітація)
        con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage=?", (VID, s))
    orch.advance(only=VID)
    r = orch.work_once()
    assert r["status"] == "done" and "r2" in r["summary"]
    assert orch.qc_verdict(VID) == "APPROVED"
    assert con.execute("SELECT status FROM fix_tasks").fetchone()[0] == "resolved"
    orch.advance(only=VID)
    assert _st(con, "approval")["status"] == "awaiting_approval"            # semi: фінал погоджує людина


def test_qc_gives_up_after_max_rounds(orch, con, monkeypatch, cfg):
    cfg.qc["max_rounds"] = 2
    fix = {"stage": "edit", "scope": "0–1 с", "description": "чорний екран", "criterion": "без чорного", "severity": "critical"}
    monkeypatch.setattr(qc_pkg, "run", lambda *a, **k: _report("BLOCKED_CRITICAL", [fix], ["G Editing: чорний екран"]))
    orch.set_mode("semi")
    for _ in range(2):
        orch.advance(only=VID)
        orch.work_once()
        if _st(con, "edit")["status"] == "pending":
            con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage='edit'", (VID,))
    st = _st(con, "qc")
    assert st["status"] == "needs_human" and "2 раунд" in st["note"]
    assert not orch.can_publish(VID)[0]
    assert con.execute("SELECT COUNT(*) FROM qc_reports").fetchone()[0] == 2


def test_qc_single_round_goes_to_owner_with_errors(orch, con, monkeypatch):
    """max_rounds = 1 (власник, 2026-11-24): без переробок — звіт на погодження; з помилками «піде» лише після
    ручного погодження, критичні (зламаний файл) блокують завжди."""
    orch.cfg.qc["max_rounds"] = 1
    fix = {"stage": "animation", "scope": "B03", "description": "статика", "criterion": "рух", "severity": "major"}
    monkeypatch.setattr(qc_pkg, "run", lambda *a, **k: _report("NEEDS_REVISION", [fix]))
    orch.set_mode("semi")
    orch.advance(only=VID)
    assert orch.work_once()["status"] == "done"
    assert _st(con, "qc")["status"] == "done" and _st(con, "animation")["status"] == "done"   # нічого не переробляється
    assert con.execute("SELECT status FROM fix_tasks").fetchone()[0] == "human"
    orch.advance(only=VID)
    assert _st(con, "approval")["status"] == "awaiting_approval"
    orch.approve(VID, "approval", who="owner")
    assert orch.can_publish(VID)[0]                                          # власник вирішив — іде з помилками


def test_qc_single_round_critical_still_blocks(orch, con, monkeypatch):
    orch.cfg.qc["max_rounds"] = 1
    fix = {"stage": "edit", "scope": "0–1 с", "description": "чорний екран", "criterion": "без чорного", "severity": "critical"}
    monkeypatch.setattr(qc_pkg, "run", lambda *a, **k: _report("BLOCKED_CRITICAL", [fix], ["G Editing: чорний екран"]))
    orch.set_mode("semi")
    orch.advance(only=VID)
    orch.work_once()
    assert _st(con, "qc")["status"] == "needs_human"
    assert not orch.can_publish(VID)[0]


# ---------- голос: зміна тексту після QC ----------
def test_voice_retires_stale_audio_without_deleting(tmp_path):
    ep = tmp_path / "ep"
    (ep / "voice").mkdir(parents=True)
    (ep / "audio/raw").mkdir(parents=True)
    (ep / "voice/elevenlabs.md").write_text("## NR\n```\nНовий текст.\n```\n", encoding="utf-8")
    (ep / "voice/requests.json").write_text(json.dumps({"requests": [{"name": "NR", "voice": "NR", "ids": ["NR-01"]}]}), encoding="utf-8")
    (ep / "voice/voices.json").write_text(json.dumps({"NR": {"voice_id": "v1"}}), encoding="utf-8")
    (ep / "audio/raw/NR.json").write_text("{}")
    (ep / "audio/raw/NR.mp3").write_text("x")
    (ep / "audio/raw/NR.src.sha1").write_text(voice._sha("Старий текст.\n", "v1"))
    assert voice.chars_needed(ep) == 0
    assert voice.retire_stale(ep, "t1") == ["NR"]
    assert (ep / "audio/raw/old-t1/NR.mp3").exists() and not (ep / "audio/raw/NR.mp3").exists()
    assert voice.chars_needed(ep) == len("Новий текст.\n")
    (ep / "audio/raw/NR.json").write_text("{}")
    voice.mark_sources(ep)
    assert voice.retire_stale(ep, "t2") == []                                # той самий текст — нічого не чіпаємо


def test_voice_keeps_owner_audio_without_mark(tmp_path):
    ep = tmp_path / "ep"
    (ep / "voice").mkdir(parents=True)
    (ep / "audio/raw").mkdir(parents=True)
    (ep / "voice/elevenlabs.md").write_text("```\nТекст.\n```\n", encoding="utf-8")
    (ep / "voice/requests.json").write_text(json.dumps({"requests": [{"name": "NR", "voice": "NR", "ids": ["NR-01"]}]}), encoding="utf-8")
    (ep / "voice/voices.json").write_text(json.dumps({"NR": {"voice_id": "v1"}}), encoding="utf-8")
    (ep / "audio/raw/NR.json").write_text("{}")
    assert voice.retire_stale(ep, "t") == [] and (ep / "audio/raw/NR.json").exists()

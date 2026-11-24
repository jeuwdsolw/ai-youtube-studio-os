"""AGENT 7 — Quality Control (етап `qc`). Перевіряє файли сам; звітам інших агентів не довіряє.

Звіт: `<епізод>/qc/qc-rN.md` + `.json`, рядок `qc_reports`, задачі `fix_tasks` (critical/major).
Цикл виправлень веде оркестратор (`Orchestrator._qc_route`): QC не пройдено → етап-виконавець отримує зауваження
«доопрацювати: QC rN — …» → повтор етапів до QC → новий раунд; після `[qc].max_rounds` — людина.
"""
from __future__ import annotations

import json

from .. import qc
from ..core import budget, clock
from ..core.db import tx
from . import common
from .base import Agent, AgentResult, BlockedError, Context, register


@register
class QCAgent(Agent):
    name = "qc"
    stage = "qc"
    resources = ("claude",)
    max_attempts = 2

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        cfg, con = ctx.cfg, ctx.con
        ep = common.episode_dir(con, cfg, ctx.video_id)
        if not ep or not ep.exists():
            raise BlockedError("у відео немає теки епізоду")
        v = con.execute("SELECT format FROM videos WHERE id=?", (ctx.video_id,)).fetchone()
        rnd = con.execute("SELECT COUNT(*) FROM qc_reports WHERE video_id=?", (ctx.video_id,)).fetchone()[0] + 1
        llm = bool(cfg.qc.get("llm_review", True))
        if llm:
            budget.require(con, cfg, "claude_sessions")
            budget.spend(con, cfg, "claude_sessions", 1, video_id=ctx.video_id)
        ctx.log("info", f"QC раунд {rnd}: автоматичні перевірки" + (" + рецензія Claude" if llm else ""))
        rep = qc.run(cfg.root, ep, v["format"], cfg, llm=llm)
        ctx.heartbeat()
        out = ep / "qc"
        out.mkdir(exist_ok=True)
        (out / f"qc-r{rnd}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / f"qc-r{rnd}.md").write_text(qc.modules.to_md(ep.name, rep, rnd), encoding="utf-8")
        rel = (out / f"qc-r{rnd}.md").relative_to(cfg.root).as_posix()
        now = clock.now_iso()
        with tx(con):
            con.execute("UPDATE fix_tasks SET status='resolved' WHERE video_id=? AND status='sent'", (ctx.video_id,))
            rid = con.execute("INSERT INTO qc_reports(video_id, round, score, verdict, critical, report, path, created_at) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (ctx.video_id, rnd, rep["score"], rep["verdict"], len(rep["critical"]),
                               json.dumps(rep, ensure_ascii=False), rel, now)).lastrowid
            for f in rep["fixes"]:
                con.execute("INSERT INTO fix_tasks(video_id, qc_report_id, stage, scope, severity, description, criterion, "
                            "created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (ctx.video_id, rid, f["stage"], f["scope"], f["severity"], f["description"], f["criterion"], now))
        return AgentResult(
            summary=f"QC r{rnd}: {rep['verdict']} · {rep['score']}/100"
                    + (f" · критичних {len(rep['critical'])}" if rep["critical"] else "")
                    + (f" · виправлень {len(rep['fixes'])}" if rep["fixes"] else "") + f" — {rel}",
            data={"verdict": rep["verdict"], "score": rep["score"], "critical": rep["critical"], "round": rnd,
                  "fixes": rep["fixes"], "report": rel, "qc_report_id": rid},
            artifacts=[rel, rel[:-3] + ".json"])

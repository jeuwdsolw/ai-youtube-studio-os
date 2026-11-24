"""AGENT 1 — Trend Research.

Звіт трендів — спільний на тиждень: етап `research` кожного відео або бере свіжий звіт, або створює новий.
Дані: YouTube Data API (якщо є ключ) → швидкість переглядів, outlier (перегляди/підписники), формат, коментарі.
Оцінка тем (0–100, 7 критеріїв ТЗ §4) — Claude Code; доказами можуть бути ЛИШЕ відео з таблиці trend_videos.
Без ключа YouTube звіт все одно створюється, але з чіткою позначкою «немає даних» і критерієм «актуальність» = null.
Google Trends і Reddit не підключені (немає офіційного доступу/ключа) — так і записується в `sources`.
"""
from __future__ import annotations

import json
from datetime import timedelta

from ..adapters import claude_code
from ..adapters.youtube_data import YouTubeData, parse_iso_duration
from ..core import budget, clock, env
from ..core.db import tx
from . import common
from .base import Agent, AgentResult, BlockedError, Context, register

CRITERIA = {  # ТЗ §4: максимум балів
    "reach": 20, "actuality": 15, "emotion": 15, "retention": 15, "originality": 15, "animation": 10, "fit": 10,
}

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"}, "description": {"type": "string"}, "why": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "plot_options": {"type": "string"},
                "scores": {"type": "object", "properties": {k: {"type": ["integer", "null"]} for k in CRITERIA},
                           "required": list(CRITERIA)},
                "format": {"type": "string", "enum": ["short", "long", "both"]},
                "title_idea": {"type": "string"}, "hook": {"type": "string"},
                "genre": {"type": "string"},
            },
            "required": ["title", "description", "why", "evidence", "plot_options", "scores", "format",
                         "title_idea", "hook", "genre"],
        }},
    },
    "required": ["summary", "topics"],
}


def collect_youtube(ctx: Context, report_id: int) -> tuple[str, list[dict], dict]:
    cfg, r = ctx.cfg, ctx.cfg.research
    yt = YouTubeData(env.get("YOUTUBE_API_KEY", cfg.root / ".env"), cfg.root / "data" / "cache" / "youtube",
                     on_cost=lambda u: budget.spend(ctx.con, cfg, "youtube_quota_units", u))
    if not yt.available():
        return "немає YOUTUBE_API_KEY у .env — дані конкурентів не зібрано", [], {}
    need = 100 * (len(r.get("queries_uk", [])) + len(r.get("queries_en", []))) + 200
    if not budget.allowed(ctx.con, cfg, "youtube_quota_units", need):
        return f"квоти YouTube на сьогодні недостатньо (потрібно ≈{need} од.) — дані не зібрано", [], {}
    now = clock.now()
    after = clock.iso(now - timedelta(days=int(r.get("lookback_days", 30))))
    found: dict[str, str] = {}
    for q in r.get("queries_uk", []):
        for vid in yt.search(q, published_after=after, max_results=int(r.get("max_results", 25)),
                             region=r.get("region"), language=r.get("language")):
            found.setdefault(vid, q)
        ctx.heartbeat()
    for q in r.get("queries_en", []):
        for vid in yt.search(q, published_after=after, max_results=int(r.get("max_results", 25))):
            found.setdefault(vid, q)
        ctx.heartbeat()
    items = yt.videos(list(found))
    chans = yt.channels(sorted({it["snippet"]["channelId"] for it in items}))
    rows = []
    for it in items:
        st, sn = it.get("statistics", {}), it["snippet"]
        pub = clock.parse(sn["publishedAt"][:19] + "Z")
        age = max(0.5, (now - pub).total_seconds() / 86400)
        views = int(st.get("viewCount", 0))
        ch = chans.get(sn["channelId"], {}).get("statistics", {})
        subs = None if ch.get("hiddenSubscriberCount") else (int(ch["subscriberCount"]) if "subscriberCount" in ch else None)
        dur = parse_iso_duration(it.get("contentDetails", {}).get("duration", ""))
        rows.append({
            "yt_id": it["id"], "query": found.get(it["id"]), "title": sn.get("title", ""),
            "channel_id": sn["channelId"], "channel_title": sn.get("channelTitle", ""), "channel_subs": subs,
            "published_at": clock.iso(pub), "age_days": round(age, 1), "views": views,
            "likes": int(st["likeCount"]) if "likeCount" in st else None,
            "comments": int(st["commentCount"]) if "commentCount" in st else None,
            "duration_s": dur, "format": "short" if 0 < dur <= 180 else "long",
            "views_per_day": round(views / age, 1),
            "outlier": round(views / subs, 2) if subs else None,
        })
    with tx(ctx.con):
        for x in rows:
            ctx.con.execute(
                "INSERT OR REPLACE INTO trend_videos(report_id, yt_id, query, title, channel_id, channel_title, channel_subs,"
                " published_at, age_days, views, likes, comments, duration_s, format, views_per_day, outlier)"
                " VALUES (:rid,:yt_id,:query,:title,:channel_id,:channel_title,:channel_subs,:published_at,:age_days,"
                ":views,:likes,:comments,:duration_s,:format,:views_per_day,:outlier)", {"rid": report_id, **x})
    comments = {}
    top = sorted(rows, key=lambda x: x["views_per_day"], reverse=True)[:10]
    for x in top:
        comments[x["yt_id"]] = yt.top_comments(x["yt_id"], int(r.get("comments_per_video", 15)))
    return f"ok: {len(rows)} відео, {yt.units} од. квоти", rows, comments


def build_prompt(ctx: Context, rows: list[dict], comments: dict, yt_status: str) -> str:
    root = ctx.cfg.root
    top = sorted(rows, key=lambda x: x["views_per_day"], reverse=True)[:60]
    table = "\n".join(
        f"{x['yt_id']} | {x['format']} | {x['duration_s']}с | {x['views']} перегл. | {x['age_days']} дн | "
        f"{x['views_per_day']}/день | підписн. {x['channel_subs'] if x['channel_subs'] is not None else 'н/д'} | "
        f"outlier {x['outlier'] if x['outlier'] is not None else 'н/д'} | {x['title']}" for x in top) or "(немає даних)"
    com = "\n".join(f"{k}: " + " / ".join(c[:160] for c in v[:8]) for k, v in comments.items()) or "(немає даних)"
    mine = "\n".join(f"- {e['title']}" for e in common.episode_titles(root))
    ideas = common.read(root.parent, "ideas.md", 6000)
    genres = ", ".join(ctx.cfg.production.get("genres", []))
    return f"""Ти — Trend Research Agent українськомовного YouTube-каналу горор-анімації «Правила виживання» (RULES OF SURVIVAL).
Канал: 2D-анімація для дорослих, історії про правила виживання, надприродне, двійників, дзвінки/повідомлення, зомбі, психологічний горор.

ДЖЕРЕЛА: YouTube — {yt_status}. Google Trends і Reddit не підключені.

ВІДЕО КОНКУРЕНТІВ (yt_id | формат | тривалість | перегляди | вік | швидкість | підписники каналу | outlier | назва):
{table}

КОМЕНТАРІ ГЛЯДАЧІВ (топ-відео за швидкістю):
{com}

ВЖЕ ЗРОБЛЕНО НА КАНАЛІ (не повторювати):
{mine}

БАНК ІДЕЙ ВЛАСНИКА (можна розвивати, це не дані трендів):
{ideas}

ЗАВДАННЯ: запропонуй {ctx.cfg.research.get('topics_per_report', 20)} оригінальних тем. Не копіюй сюжети конкурентів — лише тенденції.
Оцінки (цілі числа): reach 0–20, actuality 0–15, emotion 0–15, retention 0–15, originality 0–15, animation 0–10, fit 0–10.
- actuality оцінюй ЛИШЕ на основі наведених даних; якщо даних YouTube немає — став null.
- evidence — тільки yt_id з таблиці вище (порожній список, якщо даних немає). Не вигадуй статистику й відео.
- why: чому тема перспективна; окремо познач, що підтверджено даними, а що — твоя оцінка.
- genre — одне з: {genres}. format — short, long або both. title_idea — можлива назва (не оманлива), hook — гачок перших 3 секунд.
- Теми мають бути зрозумілі без контексту, з потенціалом продовження і цікаві для анімації. Без реальних брендів.
summary — 3–5 речень про тенденції (з позначкою, де дані, а де оцінка).
Усе українською."""


@register
class TrendResearch(Agent):
    name = "research"
    stage = "research"
    resources = ("claude",)

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        cfg = ctx.cfg
        max_age = int(cfg.research.get("report_max_age_days", 6))
        since = clock.iso(clock.now() - timedelta(days=max_age))
        row = ctx.con.execute("SELECT id, sources FROM trend_reports r WHERE created_at >= ? "
                              "AND EXISTS (SELECT 1 FROM topics t WHERE t.report_id = r.id) "   # незавершений звіт (збій Claude) — ні
                              "ORDER BY id DESC LIMIT 1", (since,)).fetchone()
        if row and env.get("YOUTUBE_API_KEY", cfg.root / ".env") and not ctx.con.execute(
                "SELECT 1 FROM trend_videos WHERE report_id=? LIMIT 1", (row["id"],)).fetchone():
            ctx.log("info", f"звіт #{row['id']} зібрано без даних YouTube, а ключ тепер є — новий звіт")
            row = None
        if row and payload.get("force") is not True:
            n = ctx.con.execute("SELECT COUNT(*) FROM topics WHERE report_id=?", (row["id"],)).fetchone()[0]
            return AgentResult(summary=f"використано звіт трендів #{row['id']} ({n} тем)",
                               data={"report_id": row["id"], "reused": True})
        budget.require(ctx.con, cfg, "claude_sessions")
        week = clock.now().astimezone(cfg.tz).strftime("%G-W%V")
        with tx(ctx.con):
            rid = ctx.con.execute("INSERT INTO trend_reports(created_at, week, sources) VALUES (?,?,?)",
                                  (clock.now_iso(), week, "{}")).lastrowid
        yt_status, rows, comments = collect_youtube(ctx, rid)
        sources = {"youtube": yt_status, "google_trends": "не підключено (офіційний API — обмежений доступ)",
                   "reddit": "не підключено (потрібен ключ застосунку)", "own_channel": "назви епізодів з episodes/*/script.md",
                   "owner_ideas": "ideas.md"}
        ctx.log("info" if rows else "warning", f"YouTube: {yt_status}")
        budget.spend(ctx.con, cfg, "claude_sessions", 1, video_id=ctx.video_id)
        res = claude_code.ask(build_prompt(ctx, rows, comments, yt_status), cwd=cfg.root, schema=TOPIC_SCHEMA,
                              model=cfg.claude.get("model_ask", "sonnet"),
                              timeout=int(cfg.claude.get("timeout_ask_s", 900)))
        out = res.structured or {}
        valid_ids = {x["yt_id"] for x in rows}
        saved = 0
        with tx(ctx.con):
            for t in out.get("topics", []):
                scores = {}
                for k, mx in CRITERIA.items():
                    v = t.get("scores", {}).get(k)
                    scores[k] = None if v is None or (k == "actuality" and not rows) else max(0, min(mx, int(v)))
                total = sum(v for v in scores.values() if v is not None)
                ev = [e for e in t.get("evidence", []) if e in valid_ids]  # жодних вигаданих доказів
                ctx.con.execute(
                    "INSERT INTO topics(report_id, title, description, why, evidence, plot_options, scores, total, format_rec,"
                    " title_idea, hook, genre, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, t["title"], t["description"], t["why"], json.dumps(ev), t["plot_options"],
                     json.dumps(scores), total, t["format"], t["title_idea"], t["hook"], t["genre"], clock.now_iso()))
                saved += 1
            ctx.con.execute("UPDATE trend_reports SET sources=?, summary=?, data=? WHERE id=?",
                            (json.dumps(sources, ensure_ascii=False), out.get("summary", ""),
                             json.dumps({"videos": len(rows), "claude_cost_usd": res.cost_usd}), rid))
        if not saved:
            raise BlockedError("Claude не запропонував жодної теми")
        return AgentResult(summary=f"звіт трендів #{rid}: {saved} тем; YouTube — {yt_status}",
                           data={"report_id": rid, "topics": saved, "sources": sources})

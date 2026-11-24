"""Етап 5 — публікація на YouTube: private + publishAt (час слоту з календаря), обкладинка, SRT.

Оркестратор запускає етап лише коли `can_publish`: QC без критичних, фінал погоджено людиною (або auto з auto_publish).
Метадані — з `youtube.md` епізоду (формат студії): назва — варіант 1, опис, теги, налаштування (аудиторія, категорія,
мова, змінений/синтетичний вміст — без явного «Так»/«Ні» етап зупиняється: це рішення власниці).
Ідемпотентність: таблиця `uploads` (URI сесії, ID відео) — повтор задачі продовжує завантаження, а не створює дубль.
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from ..adapters.youtube_upload import COST, YouTubeAuth, YouTubeUploader
from ..core import budget, clock
from ..core.db import tx
from ..qc.modules import youtube_meta
from . import common
from .base import Agent, AgentResult, BlockedError, Context, register

CATEGORIES = {"фільми й анімація": "1", "фільми та анімація": "1", "film": "1", "розваги": "24", "entertainment": "24",
              "люди й блоги": "22", "освіта": "27", "ігри": "20"}


def _setting(text: str, key: str) -> str:
    m = re.search(rf"^\s*-\s*{key}[^:\n]*:\s*(.+)$", text, re.M | re.I)
    return m.group(1).strip() if m else ""


def build_meta(ep: Path, fmt: str, publish_at: str, cfg_yt: dict) -> dict:
    y = youtube_meta(ep)
    if not y.get("titles"):
        raise BlockedError(f"немає назви у {ep.name}/youtube.md (розділ «Три варіанти назви»)")
    text = y["text"]
    title = y["titles"][0].replace("<", "").replace(">", "")[:100]
    desc = re.sub(r"[<>]", "", y.get("description", ""))[:4900]
    tags, total = [], 0
    for t in (x.strip() for x in y.get("tags", "").replace("\n", " ").split(",")):
        if t and total + len(t) + 1 <= 480:
            tags.append(t)
            total += len(t) + 1
    synth = _setting(text, "Змінений/синтетичний вміст").lower()
    if "уточнити" in synth or not re.search(r"\b(ні|так)\b", synth):
        raise BlockedError("youtube.md: «Змінений/синтетичний вміст» не вирішено (потрібно «Ні» або «Так») — рішення власниці")
    audience = _setting(text, "Аудиторія").lower()
    cat = _setting(text, "Категорія").lower()
    category = next((v for k, v in CATEGORIES.items() if k in cat), str(cfg_yt.get("category_id", "1")))
    lang = cfg_yt.get("language", "uk")
    return {
        "snippet": {"title": title, "description": desc, "tags": tags, "categoryId": category,
                    "defaultLanguage": lang, "defaultAudioLanguage": lang},
        "status": {"privacyStatus": "private", "publishAt": publish_at, "embeddable": True, "license": "youtube",
                   "selfDeclaredMadeForKids": "для дітей" in audience and "не для дітей" not in audience,
                   "containsSyntheticMedia": bool(re.search(r"\bтак\b", synth))},
    }


def _final(ep: Path) -> Path:
    f = ep / "renders" / "final_master.mp4"
    if f.exists():
        return f
    hits = sorted((ep / "renders").glob("*final_master.mp4")) if (ep / "renders").exists() else []
    if not hits:
        raise BlockedError(f"немає фіналу {ep.name}/renders/final_master.mp4")
    return hits[-1]


@register
class PublishAgent(Agent):
    name = "publish"
    stage = "publish"
    resources = ("youtube_upload",)
    max_attempts = 3

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        con, cfg, vid = ctx.con, ctx.cfg, ctx.video_id
        v = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        ep = common.episode_dir(con, cfg, vid)
        if not ep:
            raise BlockedError("у відео немає теки епізоду")
        yt = getattr(cfg, "youtube", {}) or {}
        when = clock.parse(v["publish_at_utc"])
        if when <= clock.now() + timedelta(minutes=int(yt.get("min_lead_min", 20))):
            raise BlockedError(f"час слоту {v['publish_local']} уже минув або надто близько — перенесіть відео на інший слот")
        final = _final(ep)
        meta = build_meta(ep, v["format"], clock.iso(when), yt)
        auth = YouTubeAuth(cfg.root / "data" / "youtube")
        up = YouTubeUploader(auth)
        expected = (auth.token().get("channel") or {}).get("id") or yt.get("channel_id")
        ch = up.my_channel()
        if not expected:
            raise BlockedError(f"канал не підтверджено: виконайте `python -m studio_os youtube-auth` (зараз: {ch['title']})")
        if ch["id"] != expected:
            raise BlockedError(f"токен належить іншому каналу ({ch['title']}), очікується {expected} — повторіть youtube-auth")

        row = con.execute("SELECT * FROM uploads WHERE video_id=?", (vid,)).fetchone()
        yt_id = row["yt_video_id"] if row else None
        if not yt_id:
            budget.require(con, cfg, "youtube_quota_units", COST["insert"], "завантаження відео")
            session_uri = row["session_uri"] if row and row["session_uri"] else None
            if not session_uri:
                session_uri = up.start_upload(meta, final.stat().st_size)
                budget.spend(con, cfg, "youtube_quota_units", COST["insert"], video_id=vid)
                with tx(con):   # зберегти одразу: обрив після цього → продовження, а не друге відео
                    con.execute("INSERT INTO uploads(video_id, channel_id, session_uri, publish_at, updated_at) VALUES (?,?,?,?,?) "
                                "ON CONFLICT(video_id) DO UPDATE SET session_uri=excluded.session_uri, "
                                "publish_at=excluded.publish_at, updated_at=excluded.updated_at",
                                (vid, ch["id"], session_uri, meta["status"]["publishAt"], clock.now_iso()))
            ctx.log("info", f"завантаження {final.name} ({final.stat().st_size // 1_000_000} МБ) → YouTube")
            res = up.upload(session_uri, final, progress=lambda a, b: ctx.heartbeat())
            yt_id = res["id"]
            with tx(con):
                con.execute("UPDATE uploads SET yt_video_id=?, updated_at=? WHERE video_id=?", (yt_id, clock.now_iso(), vid))
            ctx.log("info", f"відео завантажено: https://youtu.be/{yt_id}")

        row = con.execute("SELECT * FROM uploads WHERE video_id=?", (vid,)).fetchone()
        notes = []
        cover = next((p for p in (ep / "thumbnail/cover.jpg", ep / "thumbnail/cover.png") if p.exists()), None)
        if row["thumbnail"] != "ok" and cover:
            try:
                if cover.stat().st_size > 2_000_000:
                    raise BlockedError(f"{cover.name} > 2 МБ")
                up.set_thumbnail(yt_id, cover)
                budget.spend(con, cfg, "youtube_quota_units", COST["thumbnail"], video_id=vid)
                state = "ok"
            except BlockedError as e:          # напр. канал без підтвердження телефоном — відео все одно публікується
                state = str(e)[:300]
                notes.append(f"обкладинка не встановлена: {state}")
            with tx(con):
                con.execute("UPDATE uploads SET thumbnail=?, updated_at=? WHERE video_id=?", (state, clock.now_iso(), vid))
        srt = next(iter(sorted((ep / "subtitles").glob("*.srt"))), None) if (ep / "subtitles").exists() else None
        if yt.get("captions", True) and srt and row["captions"] != "ok":
            try:
                up.insert_caption(yt_id, srt, yt.get("language", "uk"))
                budget.spend(con, cfg, "youtube_quota_units", COST["caption"], video_id=vid)
                state = "ok"
            except BlockedError as e:
                state = str(e)[:300]
                notes.append(f"субтитри не додано: {state}")
            with tx(con):
                con.execute("UPDATE uploads SET captions=?, updated_at=? WHERE video_id=?", (state, clock.now_iso(), vid))

        got = up.video(yt_id) or {}
        st = got.get("status", {})
        if st.get("privacyStatus") != "private" or clock.parse(st.get("publishAt", "1970-01-01T00:00:00Z")) != when:
            raise BlockedError(f"YouTube повернув інший статус ({st.get('privacyStatus')}, publishAt={st.get('publishAt')}) — "
                               f"перевірте https://studio.youtube.com/video/{yt_id}/edit")
        for n in notes:
            ctx.log("warning", n)
        return AgentResult(
            summary=f"заплановано на {v['publish_local']} ({v['tz']}): https://youtu.be/{yt_id} — «{meta['snippet']['title']}»"
                    + (f"; {'; '.join(notes)}" if notes else ""),
            data={"yt_video_id": yt_id, "url": f"https://youtu.be/{yt_id}", "publish_at": st.get("publishAt"),
                  "channel": ch["title"], "title": meta["snippet"]["title"], "notes": notes})

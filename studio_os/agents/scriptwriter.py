"""AGENT 3 — Scriptwriter (етап `script`). Режими SHORT і LONG.

Claude Code (без інструментів, JSON за схемою) пише сценарій у форматі студії + текст озвучки по репліках + план пауз.
Агент записує: script.md, voice/elevenlabs.md, voice/requests.json, voice/voices.json, voice/voice-plan.json
(читає scripts/voice_build.py). Лінтер: штампи (повтор із попередніх відео = помилка), реальні бренди, хронометраж Shorts.
Одна автоматична переробка з конкретними зауваженнями; далі — людина. Попередня версія зберігається як script.vN.md.
"""
from __future__ import annotations

import json
import re

from ..adapters import claude_code
from ..core import budget, clock
from ..core.db import tx
from . import common
from .base import Agent, AgentResult, BlockedError, Context, register

FILTERS = {  # пресети обробки голосу (як у e17_voice_build.py); агент обирає лише з них
    "CLEAN": "highpass=f=80",
    "CLOSE": "highpass=f=70,equalizer=f=180:t=q:w=1:g=3,equalizer=f=6000:t=q:w=1.5:g=4",
    "PHONE": "highpass=f=300,lowpass=f=3400,acompressor=threshold=-20dB:ratio=4",
    "FAR": "lowpass=f=2500,aecho=0.8:0.6:60:0.25",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "латиницею, 2–4 слова через дефіс"},
        "title": {"type": "string"}, "logline": {"type": "string"}, "genre": {"type": "string"},
        "hero": {"type": "string", "description": "id канонічного персонажа або new:<опис>"},
        "locations": {"type": "array", "items": {"type": "string"}},
        "new_assets": {"type": "array", "items": {"type": "string"}},
        "script_md": {"type": "string"},
        "voice": {"type": "array", "items": {"type": "object", "properties": {
            "block": {"type": "string"}, "speaker": {"type": "string"}, "voice_key": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "text": {"type": "string"},
                "seq": {"type": "integer", "description": "наскрізний номер репліки в сюжеті (1, 2, 3…)"},
                "pre": {"type": "number"}, "filter": {"type": "string", "enum": list(FILTERS)}},
                "required": ["id", "text", "seq", "pre", "filter"]}}},
            "required": ["block", "speaker", "voice_key", "lines"]}},
    },
    "required": ["slug", "title", "logline", "genre", "hero", "locations", "new_assets", "script_md", "voice"],
}


def build_prompt(ctx: Context, video, topic, feedback: str, previous_script: str) -> str:
    root, cfg = ctx.cfg.root, ctx.cfg
    fmt = video["format"]
    wpm = cfg.production.get("words_per_minute", 140)
    lo, hi = cfg.production.get("short_target_s", [45, 75])
    if fmt == "short":
        mode = (f"SHORT MODE: 9:16, {lo}–{hi} с (≈{int(lo * wpm / 60)}–{int(hi * wpm / 60)} слів озвучки при {wpm} сл/хв). "
                "0–3 с — надзвичайно сильний гачок; 3–15 — ситуація й інтрига; 15–40 — конфлікт і нові обставини; "
                "40–60 — поворот/ескалація; кінець — сильний фінал (може бути вибір А/Б або петля).")
        guide = common.read(root, "prompts/scriptwriter.md", 18000)
        example = common.read(root, "episodes/episode-017/script.md", 9000)
    else:
        mode = ("LONG MODE: 16:9. Тривалість — скільки потребує історія, БЕЗ води заради хронометражу (рішення власника). "
                "Сильний вступ, швидке занурення, герой, конфлікт, наростання, нові загрози, повороти, кульмінація, сильна кінцівка.")
        guide = common.read(root, "channel/long-script-prompt.md", 12000)
        example = ""
    chars = "\n".join(f"- {c['id']} ({c['status']}): {c['name']} — {c['role']}" for c in common.characters(root))
    locs = "\n".join(f"- {l['id']}: {l['title']}" for l in common.locations(root))
    voices = "\n".join(f"- {k}: {v['name']}" for k, v in common.voices(root, cfg).items())
    done = "\n".join(f"- {e['title']}" for e in common.episode_titles(root))
    stock = ", ".join(f"«{s}»" for s in cfg.production.get("stock_phrases", []))
    topic_txt = (f"Тема: {topic['title']}\nОпис: {topic['description']}\nГачок: {topic['hook']}\n"
                 f"Сюжетні можливості: {topic['plot_options']}\nЖанр: {topic['genre']}\nРобоча назва: {topic['title_idea']}"
                 if topic else f"Тема (задана власником): {video['topic']}")
    redo = (f"\n\nПОПЕРЕДНЯ ВЕРСІЯ ВІДХИЛЕНА. Зауваження:\n{feedback}\n\nПопередній сценарій:\n{previous_script[:12000]}"
            if feedback else "")
    return f"""Ти — сценарист каналу «Правила виживання». Працюєш автономно: ідея й формат уже визначені, питань не став.
Мова — українська. Сетинг — Україна, побут український. Нічого не публікуй, лише поверни JSON.

{mode}

{topic_txt}

ВИМОГИ (ТЗ §6): природна розмовна мова, короткі речення, живі діалоги, мінімум експозиції, без повторів інформації,
логіка подій і мотивація персонажів, послідовність у часі й просторі, правила світу не порушуються.
Не використовуй штампи: {stock}. Без реальних брендів (вигадані назви). Без пафосних монологів.

КАНОН ПЕРСОНАЖІВ (перевага — наявним; новий персонаж → hero = "new:<опис>" і запис у new_assets):
{chars}

ЛОКАЦІЇ (перевага — наявним; нова → new_assets):
{locs}

ГОЛОСИ ElevenLabs (voice_key — ЛИШЕ з цього списку; диктор — {cfg.production.get('narrator_voice', 'NR')}, True Crime, темп {wpm} сл/хв):
{voices}
{'УВЕСЬ текст озвучує ОДИН диктор (рішення власника): репліки персонажів подавай через диктора, id — NR-01, NR-02…'
 if cfg.production.get('narrator_only', True) else ''}

ВЖЕ ЗРОБЛЕНО (не повторювати сюжети):
{done}

ФОРМАТ ВІДПОВІДІ:
- script_md — повний сценарій у форматі студії (заголовок «# Епізод — «НАЗВА» (Shorts|Long)», статус SCRIPT, формат, герой,
  далі розділи-сцени з планами й текстом диктора в «лапках»), як у прикладі нижче.
- voice — блоки озвучки (Long: блок ≤ 12 реплік; можна групувати репліки одного голосу в один блок). Кожна репліка: id (NR-01, NR-02…
  або XX-NN для персонажа), seq — НАСКРІЗНИЙ номер репліки в порядку звучання в ролику (1, 2, 3… без пропусків), text — що озвучити,
  з тегами емоцій ElevenLabs у [дужках] ([tense], [whispers], [low voice], [serious], [urgent], [fast]); цифри — словами, без лапок і ремарок.
  pre — пауза перед реплікою в секундах (дія/звук, 0–1.5); filter — CLEAN (звичайно), CLOSE (шепіт упритул), PHONE (з телефона), FAR (здалеку).
- locations — id локацій; new_assets — що треба створити (персонажі, локації, реквізит).

ДОВІДНИК СТУДІЇ (для формату; інтерактивні кроки пропусти):
{guide}

{('ПРИКЛАД ГОТОВОГО СЦЕНАРІЮ SHORTS:' + chr(10) + example) if example else ''}{redo}"""


def write_files(ep, out: dict, cfg) -> list[str]:
    (ep / "voice").mkdir(parents=True, exist_ok=True)
    old = ep / "script.md"
    if old.exists():
        n = 1 + len(list(ep.glob("script.v*.md")))
        old.rename(ep / f"script.v{n}.md")
    (ep / "script.md").write_text(out["script_md"].rstrip() + "\n", encoding="utf-8")
    catalog = common.voices(cfg.root, cfg)
    md = [f"# Озвучка — «{out['title']}» — генерація через API", "",
          "Згенеровано Studio OS (scriptwriter). Модель `eleven_v3`, `uk`. Теги [..] не озвучуються.",
          f"Генерація: `py -3.12 -X utf8 scripts/audio/elevenlabs_episode.py {ep.name}` → темп: "
          f"`py -3.12 -X utf8 scripts/voice_build.py {ep.name}`.", ""]
    reqs, used, order = [], {}, []
    n = 0
    for i, b in enumerate(out["voice"], 1):
        # унікальна назва запиту: elevenlabs_episode.py зберігає аудіо як audio/raw/<name>.* (дубль = чуже аудіо)
        name = f"{i:02d}-{re.sub(r'[^0-9A-Za-z_-]', '', b['voice_key']) or 'V'}"
        md += [f"## {name} · {b['speaker']} ({catalog.get(b['voice_key'], {}).get('name', 'ГОЛОС НЕ ВИЗНАЧЕНО')})", "```"]
        md.append("\n\n".join(ln["text"].strip() for ln in b["lines"]))
        md += ["```", ""]
        reqs.append({"name": name, "voice": b["voice_key"], "ids": [ln["id"] for ln in b["lines"]]})
        if b["voice_key"] in catalog:
            used[b["voice_key"]] = catalog[b["voice_key"]]
        for ln in b["lines"]:
            n += 1
            order.append({"id": ln["id"], "filter": ln["filter"], "pre": round(max(0.0, min(1.5, ln["pre"])), 2),
                          "_seq": ln.get("seq", n)})
    # порядок звучання — за сюжетом (seq), а не за блоками: блоки можуть бути згруповані за голосом
    order = [{k: v for k, v in o.items() if k != "_seq"} for o in sorted(order, key=lambda o: o["_seq"])]
    (ep / "voice" / "elevenlabs.md").write_text("\n".join(md), encoding="utf-8")
    (ep / "voice" / "requests.json").write_text(json.dumps(
        {"source": "elevenlabs.md", "model": "eleven_v3", "stability": 0.5, "language": "uk", "requests": reqs},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (ep / "voice" / "voices.json").write_text(json.dumps(used, ensure_ascii=False, indent=1), encoding="utf-8")
    (ep / "voice" / "voice-plan.json").write_text(json.dumps(
        {"_comment": "План монтажу голосу (Studio OS). Читає scripts/voice_build.py.",
         "pause_in": 0.18, "gap": 0.30, "filters": FILTERS, "order": order}, ensure_ascii=False, indent=1), encoding="utf-8")
    rel = ep.relative_to(cfg.root).as_posix()
    return [f"{rel}/script.md", f"{rel}/voice/elevenlabs.md", f"{rel}/voice/requests.json",
            f"{rel}/voice/voices.json", f"{rel}/voice/voice-plan.json"]


def check(out: dict, cfg, fmt: str, previous: list[str]) -> dict:
    narration = "\n".join(re.sub(r"\[[^\]]*\]", "", ln["text"]) for b in out.get("voice", []) for ln in b["lines"])
    lint = common.lint_text(out.get("script_md", "") + "\n" + narration, cfg, previous)
    words = common.count_words(narration)
    wpm = cfg.production.get("words_per_minute", 140)
    secs = words / wpm * 60
    if fmt == "short":
        lo, hi = cfg.production.get("short_target_s", [45, 75])
        if not (lo * 0.9 <= secs <= hi * 1.1):
            lint["errors"].append(f"хронометраж ≈{secs:.0f} с ({words} слів) поза межами Shorts {lo}–{hi} с")
    soft = list(lint["errors"])                      # стиль і хронометраж — у швидкому режимі лише попередження
    catalog = common.voices(cfg.root, cfg)
    for b in out.get("voice", []):
        if b["voice_key"] not in catalog:
            if cfg.production.get("narrator_only", True):
                lint["errors"].append(f"голос «{b['voice_key']}»: увесь текст має озвучувати диктор "
                                      f"{cfg.production.get('narrator_voice', 'NR')} (True Crime) — репліки персонажів переказує диктор")
            else:
                lint["warnings"].append(f"голос «{b['voice_key']}» не з каталогу — потрібен кастинг власника")
    ids = [ln["id"] for b in out.get("voice", []) for ln in b["lines"]]
    if len(ids) != len(set(ids)):
        lint["errors"].append("ID реплік повторюються")
    seqs = [ln.get("seq") for b in out.get("voice", []) for ln in b["lines"]]
    if all(s is not None for s in seqs) and sorted(seqs) != list(range(1, len(seqs) + 1)):
        lint["errors"].append("seq реплік має бути 1…N без пропусків і повторів (порядок звучання в ролику)")
    if not ids:
        lint["errors"].append("немає тексту озвучки")
    lint.update(words=words, est_seconds=round(secs, 1), hard=[e for e in lint["errors"] if e not in soft])
    return lint


@register
class Scriptwriter(Agent):
    name = "script"
    stage = "script"
    resources = ("claude",)

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        con, cfg, vid = ctx.con, ctx.cfg, ctx.video_id
        v = con.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        if not v["topic"]:
            raise BlockedError("у відео немає теми — спершу етап planning")
        topic = con.execute("SELECT * FROM topics WHERE assigned_video=? AND status='assigned'", (vid,)).fetchone()
        st = con.execute("SELECT note FROM stages WHERE video_id=? AND stage='script'", (vid,)).fetchone()
        feedback = st["note"][len("доопрацювати: "):] if st and st["note"] and st["note"].startswith("доопрацювати") else ""
        ep = common.episode_dir(con, cfg, vid)
        previous_script = (ep / "script.md").read_text(encoding="utf-8") if ep and (ep / "script.md").exists() else ""
        prev = common.recent_scripts(cfg.root)
        out, lint = None, None
        strict = cfg.production.get("strict_checks", True)
        for attempt in (1, 2):
            budget.require(con, cfg, "claude_sessions")
            budget.spend(con, cfg, "claude_sessions", 1, video_id=vid)
            res = claude_code.ask(build_prompt(ctx, v, topic, feedback, previous_script), cwd=cfg.root, schema=SCHEMA,
                                  model=cfg.claude.get("model_ask", "sonnet"), timeout=int(cfg.claude.get("timeout_ask_s", 900)))
            out = res.structured or {}
            lint = check(out, cfg, v["format"], prev)
            if not strict and not lint["hard"]:
                lint["warnings"] += lint["errors"]
                lint["errors"] = []
            if not lint["errors"]:
                break
            ctx.log("warning", f"сценарій, спроба {attempt}: " + "; ".join(lint["errors"]))
            previous_script = out.get("script_md", "")
            feedback = "Виправ ці проблеми, решту збережи:\n- " + "\n- ".join(lint["errors"])
        if lint["errors"]:
            raise BlockedError("сценарій не пройшов перевірку після переробки: " + "; ".join(lint["errors"]))
        if ep is None:
            d = common.next_short_dir(cfg.root) if v["format"] == "short" else f"long-episode-{common.slugify(out['slug'])}"
            ep = cfg.root / "episodes" / d
            if ep.exists() and any(ep.iterdir()):
                ep = cfg.root / "episodes" / f"{d}-{vid.lower()}"
            ep.mkdir(parents=True, exist_ok=True)
        arts = write_files(ep, out, cfg)
        with tx(con):
            con.execute("UPDATE videos SET episode_dir=?, title=COALESCE(title, ?), updated_at=? WHERE id=?",
                        (ep.relative_to(cfg.root).as_posix(), out["title"], clock.now_iso(), vid))
        return AgentResult(
            summary=f"сценарій «{out['title']}»: {lint['words']} слів ≈ {lint['est_seconds']} с; "
                    f"попереджень {len(lint['warnings'])}" + (f"; потрібні нові ресурси: {', '.join(out['new_assets'])}"
                                                               if out.get("new_assets") else ""),
            data={"title": out["title"], "logline": out["logline"], "hero": out["hero"], "locations": out["locations"],
                  "new_assets": out["new_assets"], "lint": lint, "episode_dir": ep.relative_to(cfg.root).as_posix()},
            artifacts=arts)

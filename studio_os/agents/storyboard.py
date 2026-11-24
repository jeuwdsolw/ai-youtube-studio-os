"""AGENT 4 — Storyboard (етап `storyboard`, після голосу: тривалості планів одразу справжні).

Claude Code (без інструментів, JSON) пише анімаційну розкадровку: план = ракурс, крупність, камера, локація,
кінець у секундах голосу, що в кадрі, біти руху (персонаж, середовище, камера). Збирання — scripts/storyboard_build.py,
перевірка повторів — tests/check_storyboard.py (R1–R5). До 2 автоматичних виправлень з переліком помилок; далі — людина.
"""
from __future__ import annotations

import json
import subprocess
import sys

from ..adapters import claude_code
from ..core import budget
from . import common
from .base import Agent, AgentResult, BlockedError, Context, register
from .voice import _load

ANGLES = ["top", "front", "back", "side", "low", "high", "ots", "pov", "insert", "eye"]
SIZES = ["ECU", "CU", "MCU", "MS", "WS", "EWS"]
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "notes": {"type": "string"},
        "tail": {"type": "number"},
        "shift": {"type": "object", "additionalProperties": {"type": "number"}},
        "scenes": {"type": "object", "additionalProperties": {"type": "string"}},
        "speakers": {"type": "object", "additionalProperties": {"type": "string"}},
        "boards": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "scene": {"type": "string"},
            "angle": {"type": "string", "enum": ANGLES}, "size": {"type": "string", "enum": SIZES},
            "camera": {"type": "string"}, "location": {"type": "string"}, "end_voice": {"type": "number"},
            "subject": {"type": "string"}, "beats": {"type": "string"},
            "card": {"type": "boolean"}, "phone": {"type": "string", "enum": ["world", "insert"]},
            "callback": {"type": "string"}, "tech": {"type": "string", "enum": ["2d", "parallax", "blender"]}},
            "required": ["id", "scene", "angle", "size", "camera", "location", "end_voice", "subject", "beats", "tech"]}},
    },
    "required": ["title", "notes", "tail", "shift", "scenes", "speakers", "boards"],
}


def prompt(ctx: Context, ep, video, feedback: str, prev_plan: str) -> str:
    root = ctx.cfg.root
    lines = json.loads((ep / "audio" / "voice-lines.json").read_text(encoding="utf-8"))
    vo = "\n".join(f"{ln['id']} {ln['start']:.2f}–{ln['end']:.2f}: {ln['text']}" for ln in lines)
    fmt = "9:16 Shorts" if video["format"] == "short" else "16:9 Long"
    locs = "\n\n".join(common.read(root, f"channel/locations/{l['id']}.md", 2500)
                       for l in common.locations(root) if l["id"] in (ep / "script.md").read_text(encoding="utf-8"))
    redo = f"\n\nПОПЕРЕДНІЙ ВАРІАНТ МАВ ПОМИЛКИ — виправ їх:\n{feedback}\n\nПопередній план:\n{prev_plan[:15000]}" if feedback else ""
    return f"""Ти — режисер розкадровки каналу «Правила виживання» ({fmt}). Поверни лише JSON за схемою.

СЦЕНАРІЙ:
{common.read(root, (ep / 'script.md').relative_to(root).as_posix(), 16000)}

ГОЛОС (готовий, секунди голосу; кінець плану end_voice задається в цих секундах):
{vo}

ПРАВИЛА РОЗКАДРОВКИ (обов'язкові):
{common.read(root, 'channel/storyboard-rules.md', 18000)}

СТИЛЬ (коротко): {common.read(root, 'channel/STYLE-GUIDE.md', 6000)}

ЛОКАЦІЇ ЕПІЗОДУ (одна модель у метрах; camera — назва точки камери в цій моделі):
{locs or '(нові локації — опиши в subject, location = новий id)'}

ВИМОГИ:
- Кожен план має осмислений рух у beats: дія персонажа (куди рухається, голова, міміка, руки, предмети), рух середовища
  (туман, пил, світло, дощ — де доречно) і камера (dolly in/out, pan, tilt, tracking, handheld — лише коли підсилює емоцію).
  Статичний план — лише свідомий прийом (напиши «статика навмисно: …» у beats).
- Сусідні плани: різний angle АБО крупність ≥ 2 кроки (R1), не дві insert поспіль (R2), різні camera (R3), angle не тричі поспіль (R4).
- Телефон у руці персонажа — phone: "world"; вставка екрана — angle insert, phone: "insert".
- end_voice зростає; останній план — після кінця останньої репліки; tail — секунди фінальних карток/петлі після голосу.
- Плани Shorts ≈ 2–4.5 с (золотий стандарт §6б), Long ≈ 3–7 с; довгу репліку ділити на кілька планів з різних ракурсів.
- tech: "2d" (≈80–90 %), "parallax" (2.5D-наїзд), "blender" (≤ 1–3 на сцену, без героя крупно).
- id планів: літера сцени + номер (A01, A02, B01…); scene: S01, S02…; scenes — назви сцен; speakers — {{"NR": "Диктор", …}}.
Усе українською.{redo}"""


def run_check(root, path) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, "-X", "utf8", str(root / "tests/check_storyboard.py"), str(path)],
                       cwd=root, capture_output=True, text=True, encoding="utf-8")
    return p.returncode == 0, p.stdout.strip()


@register
class StoryboardAgent(Agent):
    name = "storyboard"
    stage = "storyboard"
    resources = ("claude",)

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        cfg, root = ctx.cfg, ctx.cfg.root
        ep = common.episode_dir(ctx.con, cfg, ctx.video_id)
        if not ep or not (ep / "audio" / "voice-lines.json").exists():
            raise BlockedError("немає audio/voice-lines.json — спершу етап voice (audio-first)")
        video = ctx.con.execute("SELECT * FROM videos WHERE id=?", (ctx.video_id,)).fetchone()
        st = ctx.con.execute("SELECT note FROM stages WHERE video_id=? AND stage='storyboard'", (ctx.video_id,)).fetchone()
        feedback = st["note"][len("доопрацювати: "):] if st and (st["note"] or "").startswith("доопрацювати") else ""
        prev = (ep / "storyboard-plan.json").read_text(encoding="utf-8") if (ep / "storyboard-plan.json").exists() else ""
        sbb = _load(root, "scripts/storyboard_build.py", "studio_storyboard_build")
        report = ""
        for attempt in (1, 2, 3):
            budget.require(ctx.con, cfg, "claude_sessions")
            budget.spend(ctx.con, cfg, "claude_sessions", 1, video_id=ctx.video_id)
            res = claude_code.ask(prompt(ctx, ep, video, feedback, prev), cwd=root, schema=SCHEMA,
                                  model=cfg.claude.get("model_ask", "sonnet"), timeout=int(cfg.claude.get("timeout_ask_s", 900)))
            plan = res.structured or {}
            plan["aspect"] = "9:16" if video["format"] == "short" else "16:9"
            prev = json.dumps(plan, ensure_ascii=False, indent=1)
            try:
                sb = sbb.build(ep, plan)
            except SystemExit as e:
                feedback = f"Помилка збирання: {e}"
                ctx.log("warning", f"розкадровка, спроба {attempt}: {feedback}")
                continue
            (ep / "storyboard-plan.json").write_text(prev, encoding="utf-8")
            (ep / "storyboard.json").write_text(json.dumps(sb, ensure_ascii=False, indent=1), encoding="utf-8")
            (ep / "storyboard.md").write_text(sbb.to_md(plan, sb, ep.name), encoding="utf-8")
            ok, report = run_check(root, ep / "storyboard.json")
            fast = not cfg.production.get("strict_checks", True)
            if not ok and fast:
                ctx.log("warning", f"розкадровка: check_storyboard — зауваження (швидкий режим, без переробки): {report[-400:]}")
            if ok or fast:
                durs = [b["dur"] for b in sb["boards"]]
                techs = {t: sum(1 for b in plan["boards"] if b.get("tech") == t) for t in ("2d", "parallax", "blender")}
                rel = ep.relative_to(root).as_posix()
                return AgentResult(summary=f"розкадровка: {len(durs)} планів, {sum(durs):.1f} с, медіана {sorted(durs)[len(durs)//2]:.2f} с; {techs}",
                                   data={"boards": len(durs), "tech": techs, "check_ok": ok, "check": (report.splitlines() or ["—"])[-1]},
                                   artifacts=[f"{rel}/storyboard-plan.json", f"{rel}/storyboard.json", f"{rel}/storyboard.md"])
            feedback = report
            ctx.log("warning", f"розкадровка, спроба {attempt}: check_storyboard — помилки")
        raise BlockedError(f"розкадровка не пройшла перевірку після 3 спроб: {(report or feedback)[-600:]}")

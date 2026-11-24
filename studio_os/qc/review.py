"""Рецензент QC — окрема роль Claude Code (не автор): A Story, B Dialogue, C Retention, E Visual Style, H назва.

Бачить лише файли епізоду, канон і контрольні кадри фіналу (зображення переглядає інструментом Read — єдиний дозволений).
Звітам інших агентів не довіряє: оцінює те, що є у файлах і кадрах.
"""
from __future__ import annotations

from pathlib import Path

from ..adapters import claude_code
from ..agents import common

MODS = ["story", "dialogue", "retention", "style", "youtube"]
STAGES = ["script", "voice", "storyboard", "animation", "edit", "thumbnail"]

SCHEMA = {
    "type": "object",
    "required": ["summary", "scores", "issues"],
    "properties": {
        "summary": {"type": "string"},
        "scores": {"type": "object", "required": MODS,
                   "properties": {m: {"type": "integer", "minimum": 0, "maximum": 100} for m in MODS}},
        "issues": {"type": "array", "items": {
            "type": "object", "required": ["module", "severity", "msg", "stage", "scope", "criterion"],
            "properties": {"module": {"enum": MODS}, "severity": {"enum": ["critical", "major", "minor"]},
                           "msg": {"type": "string"}, "stage": {"enum": STAGES}, "scope": {"type": "string"},
                           "criterion": {"type": "string"}}}},
    },
}

SYSTEM = ("Ти — незалежний рецензент якості студії «Правила виживання» (горор-анімація 2D/2.5D для дорослих, українською). "
          "Ти не автор цього епізоду. Будь суворим і конкретним: кожна проблема — що саме, де (план/секунда/репліка), "
          "який етап виправляє і як перевірити виправлення. Не вигадуй проблем, яких не бачиш у файлах чи кадрах; "
          "не хвали. Відповідай українською.")

CRITICAL_RULES = """Критичні (severity=critical) — лише такі:
- story: сюжетна діра (подія без причини, яка ламає історію) або порушене правило світу з channel/continuity.md;
- retention: слабкий хук у Shorts (перші 3 с не створюють питання/загрози);
- style: фотореалістичний кадр, чужий/неканонічний персонаж замість канонічного, різкий стрибок стилю між планами, AI-подібні зображення;
- youtube: оманлива назва (обіцяє те, чого немає у відео).
Решта — major (помітно глядачу) або minor (дрібниця)."""


def images(root: Path, ep: Path, frames_dir: str | None, hero_ids: list[str]) -> list[str]:
    out = []
    if frames_dir and Path(frames_dir).exists():
        out += [p.relative_to(root).as_posix() for p in sorted(Path(frames_dir).glob("*.png"))[:9]]
    for n in ("small.png", "cover_360.png"):
        if (ep / "thumbnail" / n).exists():
            out.append((ep / "thumbnail" / n).relative_to(root).as_posix())
            break
    for h in hero_ids:
        out += [p.relative_to(root).as_posix() for p in sorted((root / "channel/characters" / h / "2d").glob("*Views.png"))[:1]]
    return out


def hero_ids(root: Path, ep: Path) -> list[str]:
    text = common.read(root, ep.relative_to(root).as_posix() + "/script.md").lower()
    return [c["id"] for c in common.characters(root) if c["id"].lower() in text or (c["name"] and c["name"].lower() in text)][:3]


def review(root: Path, ep: Path, fmt: str, cfg, auto: dict) -> dict:
    rel = ep.relative_to(root).as_posix()
    imgs = images(root, ep, auto.get("frames_dir"), hero_ids(root, ep))
    auto_lines = "\n".join(f"- [{i['module']}/{i['severity']}] {i['msg']}" for i in auto["issues"]) or "- (немає)"
    prompt = f"""Перевір якість готового епізоду `{rel}` ({'Shorts 9:16' if fmt == 'short' else 'Long 16:9'}).

Матеріали (текст):
=== script.md ===
{common.read(root, rel + '/script.md', 12000)}
=== storyboard.md (скорочено) ===
{common.read(root, rel + '/storyboard.md', 6000)}
=== youtube.md ===
{common.read(root, rel + '/youtube.md', 5000)}
=== channel/continuity.md (правила світу, скорочено) ===
{common.read(root, 'channel/continuity.md', 6000)}
=== channel/STYLE-GUIDE.md (скорочено) ===
{common.read(root, 'channel/STYLE-GUIDE.md', 5000)}

Зображення — переглянь КОЖНЕ інструментом Read (шляхи від поточної теки):
{chr(10).join('- ' + i for i in imgs) or '- (кадрів немає)'}
(контрольні кадри фіналу; мініатюра обкладинки; аркуші канонічних персонажів для порівняння).

Автоматичні перевірки вже знайшли (не дублюй їх, але врахуй у балах):
{auto_lines}

Оціни 0–100 модулі: story (логіка, причини/наслідки, правила світу), dialogue (природність тексту диктора/реплік, штампи),
retention (хук, щільність подій, «вода»; Long — без розтягування заради хронометражу), style (відповідність STYLE-GUIDE і канону
на кадрах: 2D/2.5D, палітра, персонажі як на аркушах, читабельність, немає фотореалізму), youtube (назви не оманливі, опис по суті).
{CRITICAL_RULES}
Етап для виправлення: script (текст/сюжет), voice (озвучка), storyboard (кадри), animation (рух/вигляд планів), edit (монтаж, звук,
субтитри, youtube.md), thumbnail (обкладинка)."""
    res = claude_code.ask(prompt, cwd=root, schema=SCHEMA, model=cfg.claude.get("model_ask", "sonnet"), system=SYSTEM,
                          timeout=int(cfg.claude.get("timeout_ask_s", 900)), read_only=True)
    out = res.structured or {}
    out["issues"] = [i for i in out.get("issues", []) if i.get("module") in MODS and i.get("stage") in STAGES]
    out["images"] = imgs
    return out

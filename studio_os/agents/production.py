"""AGENT 5 — Animation Production, Edit/Assemble і Thumbnail — агентні сесії Claude Code в теці студії.

Анімація будується ТІЛЬКИ наявними засобами каналу (SVG-риги, локації, Remotion, Blender для 2.5D) — AI-генерація
зображень заборонена. Після сесії агент сам перевіряє результат машинно і не вірить звіту сесії на слово:
- animation: композиції є в Root.tsx; для Shorts (episode-NNN) — motion_check.mjs на всі сцени (STATIC/HOLD + написи);
- edit: renders/final_master.mp4 існує й проходить tests/check_video.py (формат, гучність −14 LUFS, статика);
- thumbnail (Long): крок 1 — чорнова без тексту + gpt-prompt.md → чекаємо файл власника cover-gpt.*;
  крок 2 — текст як на референсі «Як справи» v2 → cover.jpg + cover_360.png;
- thumbnail (Shorts, власник 2026-11-23: без ChatGPT): одна сесія, як у студії для еп. 009–018 — композиція `EpNNNThumb`
  1080×1920 з текстом v4 (`ThumbText.tsx`) → cover.png + cover.jpg + small.png (360×640).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

from . import common
from .agentic import RESULT_SCHEMA, ffprobe_size, missing_compositions, run_node, session
from .base import Agent, AgentResult, BlockedError, Context, RetryableError, register

WAIT_PREFIX = "WAIT_FILE:"


def _ep(ctx: Context):
    ep = common.episode_dir(ctx.con, ctx.cfg, ctx.video_id)
    if not ep:
        raise BlockedError("у відео ще немає теки епізоду (етап script)")
    v = ctx.con.execute("SELECT * FROM videos WHERE id=?", (ctx.video_id,)).fetchone()
    return ep, v


def _feedback(ctx: Context, stage: str) -> str:
    st = ctx.con.execute("SELECT note FROM stages WHERE video_id=? AND stage=?", (ctx.video_id, stage)).fetchone()
    n = (st["note"] or "") if st else ""
    return n[len("доопрацювати: "):] if n.startswith("доопрацювати") else ""


@register
class AnimationAgent(Agent):
    name = "animation"
    stage = "animation"
    resources = ("claude", "remotion")
    max_attempts = 2

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        ep, v = _ep(ctx)
        root = ctx.cfg.root
        if not (ep / "storyboard.json").exists():
            raise BlockedError("немає storyboard.json — спершу етап storyboard")
        sb = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
        scenes = sorted({b["scene"] for b in sb["boards"]})
        m = re.fullmatch(r"episode-(\d+)", ep.name)
        prefix_hint = f"E{int(m.group(1))}Shot" if m else "(обери коротку назву, напр. LxShot)"
        fb = _feedback(ctx, "animation")
        strict = ctx.cfg.production.get("strict_checks", True)
        rel_ep = ep.relative_to(root).as_posix()
        check_step = (
            f"5. Перевір: `py -3.12 scripts/in_video.py node scripts/motion_check.mjs {prefix_hint} ../{rel_ep}/previews/motion "
            f"{' '.join(scenes)}`\n   (для Shorts) — STATIC 0, помилок написів 0. Виправ і повтори. Переглядай аркуші сцен, "
            "а не кожен кадр (економія)." if strict else
            "5. ШВИДКИЙ РЕЖИМ: motion_check НЕ запускай — його один раз запустить конвеєр після сесії. Жодних циклів\n"
            "   «перевір → виправ → перевір»: один раз `py -3.12 scripts/in_video.py npx tsc --noEmit` і, якщо сумніваєшся "
            "в плані, один still.")
        task = f"""Анімуй епізод `{ep.relative_to(root).as_posix()}` ({'Shorts 9:16' if v['format'] == 'short' else 'Long 16:9'}) за затвердженою розкадровкою.

Порядок роботи — як у студії (CLAUDE.md, channel/production-optimization.md, скіл studio-animation):
1. Прочитай script.md, storyboard.json/.md, audio/voice-lines.json, scene-memory.md (створи, якщо нема), локації й канон персонажів.
2. Створи код планів у `video/src/episodes/<код епізоду>/` (kit.tsx, cast.tsx, локація, sc1…scN.tsx ≤ 250 рядків),
   використовуючи наявні риги, пози `two-d/rig/poses.ts`, шаблони `episodes/plans.tsx`, хода `walk.ts`. Нових персонажів/локацій
   у канон НЕ додавай — якщо їх бракує, зупинись (blockers).
3. Композиції кожного плану `{prefix_hint}<ID>` і всього епізоду — у video/src/Root.tsx (за зразком Ep017/E17Shot).
4. Кожен план має осмислений рух (персонаж, середовище, камера); плани 2.5D Blender — скриптами scripts/blender/ (якщо є в розкадровці).
{check_step}
6. Онови scene-memory.md і status.md епізоду. Не рендери фінал (це етап edit).
{('ЗАУВАЖЕННЯ ВЛАСНИКА ДО ПОПЕРЕДНЬОЇ ВЕРСІЇ: ' + fb) if fb else ''}
Поверни JSON: summary, files, compositions, shot_prefix, scenes, checks (що й з яким результатом запускав), blockers."""
        out = session(ctx, task, RESULT_SCHEMA)
        prefix = out.get("shot_prefix") or prefix_hint
        missing = missing_compositions(root, out.get("compositions", []))
        if missing or not out.get("compositions"):
            raise RetryableError(f"композицій немає в Root.tsx: {missing or 'жодної не повідомлено'}")
        checks = {"motion_check": "пропущено (Long — перевірка відео на етапі QC)"}
        if m:
            code, log = run_node(root, ["scripts/motion_check.mjs", prefix,
                                        f"../{ep.relative_to(root).as_posix()}/previews/motion", *scenes], timeout=5400)
            checks["motion_check"] = log.splitlines()[-1] if log else f"код {code}"
            if code != 0 and strict:
                raise RetryableError(f"motion_check не пройдено (STATIC або написи): {log[-800:]}")
            if code != 0:
                ctx.log("warning", f"motion_check: є зауваження (швидкий режим — без переробки): {log[-600:]}")
        return AgentResult(summary=f"анімація: {len(out.get('compositions', []))} композицій; motion_check: {checks['motion_check']}",
                           data={"prefix": prefix, "compositions": out.get("compositions"), "checks": checks,
                                 "session_checks": out.get("checks")}, artifacts=out.get("files", []))


@register
class EditAgent(Agent):
    name = "edit"
    stage = "edit"
    resources = ("claude", "remotion")
    max_attempts = 2

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        ep, v = _ep(ctx)
        root = ctx.cfg.root
        rel = ep.relative_to(root).as_posix()
        fb = _feedback(ctx, "edit")
        strict = ctx.cfg.production.get("strict_checks", True)
        check_step = (f"4. Перевір: `py -3.12 tests/check_video.py {rel}/renders/final_master.mp4` — має бути PASS. "
                      "Онови status.md." if strict else
                      "4. ШВИДКИЙ РЕЖИМ: check_video НЕ запускай — конвеєр перевірить фінал один раз. Один рендер, "
                      "без повторів. Онови status.md.")
        task = f"""Змонтуй і відрендери епізод `{rel}` ({'Shorts 1080×1920' if v['format'] == 'short' else 'Long 1920×1080'}), анімація вже готова.
1. Монтажний лист edit.json, voice-timeline і субтитри з storyboard.json + audio/ (за зразком scripts/e17_edit_build.py; новий скрипт
   назви за кодом епізоду). Звуки — лише наявні процедурні SFX студії (scripts/audio/gen_sfx*.py, assets/sounds/), без сторонніх ліцензій.
   {"МУЗИКИ НЕ ДОДАВАЙ (Shorts — без музики, власниця 2026-11-24): лише голос, SFX, фони, дрон." if v['format'] == 'short' else
    "Музика — лише вже наявні в студії треки (assets/music, video/public/music), ducking під голос."}
2. Рендер `{rel}/renders/final.mp4` (Remotion, один процес), мастер: `py -3.12 -X utf8 scripts/audio/master.py {rel}/renders/final.mp4`.
3. SRT: `py -3.12 scripts/export_srt.py {ep.name}`. youtube.md — 3 варіанти назви (не оманливі, без повтору назв каналу), опис, теги,
   налаштування (як у episodes/episode-017/youtube.md), розділ «Використані матеріали».
{check_step}
{('ЗАУВАЖЕННЯ: ' + fb) if fb else ''}
Поверни JSON: summary, files, compositions, checks, blockers."""
        out = session(ctx, task, RESULT_SCHEMA)
        final = ep / "renders" / "final_master.mp4"
        if not final.exists():
            raise RetryableError("немає renders/final_master.mp4 після сесії")
        p = subprocess.run([sys.executable, "-X", "utf8", str(root / "tests/check_video.py"), str(final)],
                           cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=1800)
        passed = p.returncode == 0
        if not passed and strict:
            raise RetryableError(f"check_video не пройдено: {(p.stdout + p.stderr)[-800:]}")
        if not passed:
            ctx.log("warning", f"check_video: зауваження (швидкий режим — далі QC): {(p.stdout + p.stderr)[-600:]}")
        size = ffprobe_size(final)
        want = (1080, 1920) if v["format"] == "short" else (1920, 1080)
        if size != want:
            raise RetryableError(f"роздільність {size}, очікується {want}")
        return AgentResult(summary=f"монтаж: {rel}/renders/final_master.mp4 {size[0]}×{size[1]}, "
                                   f"check_video {'PASS' if passed else 'із зауваженнями'}",
                           data={"check_video": p.stdout[-1500:], "session_checks": out.get("checks")},
                           artifacts=[f"{rel}/renders/final_master.mp4", f"{rel}/youtube.md"])


def gpt_cover(ep):
    hits = sorted((ep / "thumbnail").glob("cover-gpt.*")) if (ep / "thumbnail").exists() else []
    return hits[-1] if hits else None


@register
class ThumbnailAgent(Agent):
    name = "thumbnail"
    stage = "thumbnail"
    resources = ("claude", "remotion")
    max_attempts = 2

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        ep, v = _ep(ctx)
        root = ctx.cfg.root
        rel = ep.relative_to(root).as_posix()
        size = "1920×1080" if v["format"] == "long" else "1080×1920"
        if v["format"] == "short":
            return self._short(ctx, ep, rel)
        gpt = gpt_cover(ep)
        if gpt is None:
            if not (ep / "thumbnail" / "cover-notext.png").exists():
                task = f"""Крок 1 обкладинки епізоду `{rel}` ({size}) — ЧОРНОВА БЕЗ ТЕКСТУ (процес власника, channel/STYLE-GUIDE.md §11).
- Композиція Thumb у коді епізоду за еталоном channel/branding/thumbnail-standard.md: сюжетний момент, герой (канонічний риг),
  загроза близько, одне світло загрози, ліва половина (або верх для 9:16) — вільна під текст, правий нижній кут вільний.
- Рендер `{rel}/thumbnail/cover-notext.png` ({size}). Жодного тексту на зображенні.
- `{rel}/thumbnail/gpt-prompt.md` — промпт для ChatGPT за зразком episodes/medohliad/thumbnail/chatgpt-prompt.md:
  що ЗБЕРЕГТИ (композицію, стиль 2D, зовнішність персонажів за каноном, вільну зону, формат, без тексту), що ПОКРАЩИТИ
  (світло, кольорокорекція, глибина, «цікавинки»).
Поверни JSON: summary, files, compositions, checks, blockers."""
                session(ctx, task, RESULT_SCHEMA)
            if not (ep / "thumbnail" / "cover-notext.png").exists():
                raise RetryableError("немає thumbnail/cover-notext.png після сесії")
            raise BlockedError(f"{WAIT_PREFIX} {rel}/thumbnail/cover-gpt.png — доопрацюйте cover-notext.png у ChatGPT "
                               f"за thumbnail/gpt-prompt.md і покладіть результат як cover-gpt.png")
        task = f"""Крок 3 обкладинки епізоду `{rel}`: фон — `{gpt.relative_to(root).as_posix()}` (доопрацьований власником у ChatGPT).
Додай текст як на референсі «Як справи» v2 (`scripts/ys_thumb_text.py`, channel/STYLE-GUIDE.md §11): ключове слово величезним білим
Rubik 900 з глітчем, підзаголовок warning.glow, червона риска, кікер — червона плашка; текст у вільній зоні, правий нижній кут вільний.
Текст — з назви й гачка епізоду (script.md, youtube.md). Скрипт назви за кодом епізоду (не змінюй ys_thumb_text.py).
Спершу перевір, що фон ChatGPT не змінив персонажів і не став фотореалістичним — інакше blockers.
Результат: `{rel}/thumbnail/cover.jpg` ({size}, JPEG < 2 МБ), `{rel}/thumbnail/cover.png`, перевірка `{rel}/thumbnail/cover_360.png`.
Поверни JSON: summary, files, compositions, checks, blockers."""
        session(ctx, task, RESULT_SCHEMA)
        return _verify_cover(ep, rel, (1920, 1080), "cover_360.png")

    def _short(self, ctx: Context, ep, rel: str) -> AgentResult:
        m = re.fullmatch(r"episode-(\d+)", ep.name)
        comp = f"Ep{int(m.group(1)):03d}Thumb" if m else "ShortThumb"
        fb = _feedback(ctx, "thumbnail")
        task = f"""Обкладинка Shorts епізоду `{rel}` (1080×1920) — як у студії для еп. 009–018, БЕЗ кроку ChatGPT (рішення власника).
- Еталон: channel/branding/thumbnail-standard.md (розділ v4) + зразок video/src/episodes/ep017/Thumb.tsx (композиція Ep017Thumb):
  сюжет одним кадром (предмет/ситуація-гачок, а не «герой ліворуч — монстр праворуч»), 3 плани без перекриття
  (джерело загрози → світло → предмет-гачок), канонічні риги й локація епізоду, палітра каналу, фон яскравіший за кадри серії.
- Текст — лише `video/src/components/ThumbText.tsx` (ThumbLine, Kicker, THUMB_ACCENT): білий капс Rubik 900, одне жовте слово,
  кікер-плашка; угорі, низ ≈ 20 % вільний (інтерфейс Shorts). Текст — з назви й гачка script.md, не повторює назву дослівно.
- Композиція `{comp}` (1080×1920, 1 кадр) у коді епізоду + рядок у video/src/Root.tsx.
- Рендер: `py -3.12 scripts/in_video.py npx remotion still {comp} ../{rel}/thumbnail/cover.png`;
  `ffmpeg -y -i {rel}/thumbnail/cover.png -q:v 2 {rel}/thumbnail/cover.jpg` (< 2 МБ);
  `ffmpeg -y -i {rel}/thumbnail/cover.png -vf scale=360:-1 {rel}/thumbnail/small.png` — переглянь small.png: заголовок, кікер і гачок читаються.
- Перевір межі написів: `py -3.12 scripts/in_video.py node scripts/audit_text.mjs …` за зразком CLAUDE.md (або board_check.mjs) — нічого не виходить за кадр.
{('ЗАУВАЖЕННЯ ВЛАСНИКА: ' + fb) if fb else ''}
Поверни JSON: summary, files, compositions, checks, blockers."""
        out = session(ctx, task, RESULT_SCHEMA)
        res = _verify_cover(ep, rel, (1080, 1920), "small.png")
        res.data["composition"] = comp
        res.data["session_checks"] = out.get("checks")
        return res


def _verify_cover(ep, rel: str, want: tuple[int, int], small: str) -> AgentResult:
    cover = ep / "thumbnail" / "cover.jpg"
    if not cover.exists():
        raise RetryableError("немає thumbnail/cover.jpg після сесії")
    if cover.stat().st_size > 2_000_000:
        raise RetryableError("cover.jpg більший за 2 МБ")
    got = ffprobe_size(cover)
    if got != want:
        raise RetryableError(f"cover.jpg {got}, очікується {want}")
    if not (ep / "thumbnail" / small).exists():
        raise RetryableError(f"немає thumbnail/{small} (перевірка читабельності в мініатюрі)")
    return AgentResult(summary=f"обкладинка готова: {rel}/thumbnail/cover.jpg {got[0]}×{got[1]}",
                       artifacts=[f"{rel}/thumbnail/cover.jpg", f"{rel}/thumbnail/{small}"])

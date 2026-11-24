"""Агентні сесії Claude Code (анімація, монтаж, обкладинка) зі спільними запобіжниками.

- бюджет `claude_sessions`, тайм-аут, ресурсні замки (`claude`, `remotion`) — через чергу;
- захист канону: якщо сесія змінила файли в PROTECTED (канон персонажів, риги, палітра, біблія стилю) —
  етап зупиняється для перевірки людиною (зміни НЕ відкочуються автоматично — це можуть бути й ваші правки);
- результат сесії — JSON за схемою (що зроблено, файли, композиції), далі агент сам перевіряє його машинно.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..adapters import claude_code
from ..core import budget
from .base import BlockedError, Context

PROTECTED = ["channel/palette.json", "channel/style-bible.md", "channel/STYLE-GUIDE.md", "channel/continuity.md",
             "channel/storyboard-rules.md", "channel/characters", "video/src/two-d/rig", "video/src/lib/palette.ts"]

SYSTEM = ("Ти працюєш автономно як частина Studio OS студії «Правила виживання». Людини поруч немає: не став питань, "
          "не чекай підтверджень, не комітить у git. Дотримуйся CLAUDE.md, channel/STYLE-GUIDE.md і скіла studio-animation. "
          "Не змінюй канон (channel/, риги video/src/two-d/rig/, палітру) — якщо без цього не обійтися, зупинись і поясни в "
          "полі blockers. AI-генерація зображень заборонена. Не повідомляй «готово», якщо перевірки фактично не пройдені. "
          "КОМАНДИ: запускай лише з кореня студії, НІКОЛИ не використовуй cd/Set-Location і ланцюжки (;, &&). Усе, що треба "
          "виконати в теці video/, — через `py -3.12 scripts/in_video.py <node|npx remotion|npx tsc> …` (напр. "
          "`py -3.12 scripts/in_video.py node scripts/motion_check.mjs …`, `py -3.12 scripts/in_video.py npx remotion still …`, "
          "`py -3.12 scripts/in_video.py npx tsc --noEmit`); Python — `py -3.12 -X utf8 <скрипт>`; також ffmpeg/ffprobe. "
          "Інші команди заблоковані. "
          "ЗВІТ: blockers — ЛИШЕ коли результат етапу здати неможливо (бракує персонажа/локації, треба змінити канон, "
          "перевірка не проходить). Зауваження, сумніви й рішення, які варто показати власниці, але які не заважають здати "
          "етап, — у поле owner_notes (конвеєр не зупиняють).")


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for rel in PROTECTED:
        p = root / rel
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()) if p.exists() else []
        for f in files:
            out[f.relative_to(root).as_posix()] = hashlib.sha1(f.read_bytes()).hexdigest()
    return out


def changed(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def _bundles() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("remotion-webpack-bundle-*"))


def remotion_busy() -> bool:
    """Чи працює зараз будь-який процес Remotion (напр. рендер власниці). Не вдалося перевірити — вважаємо, що так."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | ForEach-Object CommandLine"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
        else:
            out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    return "remotion" in out.lower()


@contextmanager
def remotion_temp():
    """Remotion на Windows лишає в Temp тимчасовий бандл ~0.5 ГБ на кожен still/render/compositions (за день сесій —
    ~10 ГБ, і важкі задачі зупиняються через диск). Прибираємо лише бандли, що з'явилися за цей блок, і лише коли
    жоден процес Remotion не працює."""
    before = _bundles()
    try:
        yield
    finally:
        new = _bundles() - before
        if new and not remotion_busy():
            for d in new:
                shutil.rmtree(d, ignore_errors=True)


def session(ctx: Context, task: str, schema: dict) -> dict:
    cfg, root = ctx.cfg, ctx.cfg.root
    budget.require(ctx.con, cfg, "claude_sessions")
    before = snapshot(root)
    budget.spend(ctx.con, cfg, "claude_sessions", 1, video_id=ctx.video_id)
    ctx.log("info", "агентна сесія Claude Code — старт")
    with remotion_temp():
        res = claude_code.work(task, cwd=root, model=cfg.claude.get("model_work", "opus"), system=SYSTEM, schema=schema,
                               timeout=int(cfg.claude.get("timeout_work_s", 14400)))
    diff = changed(before, snapshot(root))
    out = res.structured or {}
    ctx.log("info", f"сесія завершена: {res.turns} кроків; {out.get('summary', '')[:300]}")
    if diff:
        raise BlockedError("сесія змінила канон — перевірте вручну: " + ", ".join(diff[:10]))
    for n in out.get("owner_notes") or []:
        ctx.log("warning", f"для власниці: {n[:500]}")
    if out.get("blockers"):
        raise BlockedError("агент зупинився: " + "; ".join(out["blockers"])[:800])
    return out


def run_node(root: Path, args: list[str], timeout: int = 3600) -> tuple[int, str]:
    with remotion_temp():
        p = subprocess.run(["node", *args], cwd=root / "video", capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout, shell=False)
    return p.returncode, (p.stdout + p.stderr)[-3000:]


def registered_compositions(root: Path, timeout: int = 300) -> set[str] | None:
    """Справжній список композицій Remotion (`npx remotion compositions --quiet`): у Root.tsx вони часто
    створюються циклом (`id={`E19Shot${b.id}`}`), тож пошук рядка в тексті дає хибне «немає». None — Remotion недоступний."""
    npx = shutil.which("npx")
    if not npx or not (root / "video/package.json").exists():
        return None
    try:
        with remotion_temp():
            p = subprocess.run([npx, "--no-install", "remotion", "compositions", "--quiet"], cwd=root / "video",
                               capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    ids = set(p.stdout.split())
    return ids if p.returncode == 0 and ids else None


def missing_compositions(root: Path, ids: list[str]) -> list[str]:
    """Сесія іноді дописує коментар до назви («Ep019 (existing; …)») — порівнюємо лише сам ідентифікатор."""
    import re
    ids = [m.group(0) for c in ids if (m := re.match(r"[A-Za-z][A-Za-z0-9_-]*", str(c).strip()))]
    real = registered_compositions(root)
    if real is not None:
        return [c for c in ids if c not in real]
    rootx = (root / "video/src/Root.tsx").read_text(encoding="utf-8")
    return [c for c in ids if c not in rootx]


def ffprobe_size(path: Path) -> tuple[int, int] | None:
    from .maintenance import find_ffprobe
    fp = find_ffprobe()
    if not fp or not path.exists():
        return None
    out = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return None


RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "compositions": {"type": "array", "items": {"type": "string", "description": "лише ID композиції, без коментарів"}},
        "shot_prefix": {"type": "string"},
        "scenes": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"},
                     "description": "лише те, що не дає здати етап; інакше — порожній масив"},
        "owner_notes": {"type": "array", "items": {"type": "string"},
                        "description": "зауваження для власниці, які не зупиняють конвеєр"},
    },
    "required": ["summary", "files", "compositions", "checks", "blockers"],
}

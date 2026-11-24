"""Claude Code у безголовому режимі (`claude -p`) — єдиний LLM студії (рішення власника: лише підписка).

Два види викликів:
- `ask(...)`  — текстова/структурована відповідь без інструментів (`--tools ""`, `--json-schema`);
- `work(...)` — агентна сесія в теці студії: читає CLAUDE.md і скіли проєкту, редагує файли, запускає дозволені команди.

Кожен виклик рахується в бюджеті `claude_sessions`. Ліміт використання підписки → RetryableError (черга повторить пізніше).
Бінарник можна підмінити змінною STUDIO_CLAUDE_BIN (тести).
"""
from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..errors import BlockedError, LimitError, RetryableError

LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "hit your", "-hour limit", "weekly limit",
                 "overloaded", "429", "529", "try again")


def reset_time(msg: str, now=None):
    """«… resets 2am (Europe/Kyiv)» / «resets 2:30pm» → найближчий такий момент у UTC; None, якщо не розпізнано."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", msg, re.I)
    if not m:
        return None
    h, mi, ap, tzname = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    try:
        tz = ZoneInfo(tzname) if tzname else timezone.utc
    except Exception:  # noqa: BLE001 — невідома зона
        tz = timezone.utc
    from ..core import clock
    now = (now or clock.now()).astimezone(tz)   # годинник студії (у тестах — підставний)
    t = now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.astimezone(timezone.utc)

# Інструменти агентної сесії студії. Bash — лише інструменти конвеєра (без git push, без видалень).
# Команди без зміни теки (Claude Code завжди питає людину про `cd`): усе для video/ — через scripts/in_video.py.
# На Windows фонові сесії виконують команди інструментом PowerShell, деінде — Bash: правила для обох.
_CMDS = ["node *", "py -3.12 *", "ffmpeg *", "ffprobe *", "tools/studio-venv/Scripts/python.exe *"]
WORK_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Skill"] + [f"{t}({c})" for t in ("PowerShell", "Bash") for c in _CMDS]


@dataclass
class ClaudeResult:
    text: str
    structured: dict | None
    cost_usd: float
    turns: int
    session_id: str
    raw: dict


def binary() -> str | None:
    return os.environ.get("STUDIO_CLAUDE_BIN") or shutil.which("claude")


def health() -> tuple[bool, str]:
    b = binary()
    if not b:
        return False, "claude CLI не знайдено в PATH"
    try:
        v = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        return True, v
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"claude не запускається: {e}"


def _run(args: list[str], prompt: str, cwd: Path, timeout: int) -> ClaudeResult:
    b = binary()
    if not b:
        raise BlockedError("Claude Code CLI не встановлено або не в PATH")
    exe = [sys.executable, "-X", "utf8", b] if b.endswith(".py") else [b]   # .py — підставний бінарник у тестах
    cmd = [*exe, "-p", "--output-format", "json", "--no-session-persistence", *args]
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           cwd=str(cwd), timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RetryableError(f"claude -p перевищив тайм-аут {timeout} с") from e
    out = (p.stdout or "").strip()
    try:
        raw = json.loads(out.splitlines()[-1] if out else "{}")
    except json.JSONDecodeError:
        raw = {}
    msg = (raw.get("result") or p.stderr or out or "").strip()
    if p.returncode != 0 or raw.get("is_error") or not raw:
        low = msg.lower()
        if any(m in low for m in LIMIT_MARKERS):
            raise LimitError(f"ліміт Claude: {msg[:300]}", reset_time(msg))
        if "login" in low or "authenticat" in low:
            raise BlockedError(f"Claude Code не авторизовано: {msg[:300]}")
        raise RetryableError(f"claude -p помилка (код {p.returncode}): {msg[:500]}")
    return ClaudeResult(text=raw.get("result") or "", structured=raw.get("structured_output"),
                        cost_usd=float(raw.get("total_cost_usd") or 0), turns=int(raw.get("num_turns") or 0),
                        session_id=raw.get("session_id", ""), raw=raw)


def ask(prompt: str, *, cwd: Path, schema: dict | None = None, model: str = "sonnet",
        system: str | None = None, timeout: int = 900, read_only: bool = False) -> ClaudeResult:
    """Структурована відповідь. read_only=True — дозволено лише Read (напр. рецензент QC переглядає кадри-зображення)."""
    args = ["--model", model] + (["--tools", "Read", "--allowedTools", "Read"] if read_only else ["--tools", ""])
    if schema:
        args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    if system:
        args += ["--append-system-prompt", system]
    res = _run(args, prompt, cwd, timeout)
    if schema and not isinstance(res.structured, dict):
        try:
            res.structured = json.loads(res.text)
        except json.JSONDecodeError as e:
            raise RetryableError("Claude не повернув JSON за схемою") from e
    return res


def work(prompt: str, *, cwd: Path, model: str = "opus", tools: list[str] | None = None,
         system: str | None = None, timeout: int = 4 * 3600, schema: dict | None = None) -> ClaudeResult:
    args = ["--model", model, "--permission-mode", "acceptEdits",
            "--allowedTools", ",".join(tools or WORK_TOOLS)]   # через кому: у правилах є пробіли
    if schema:
        args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    if system:
        args += ["--append-system-prompt", system]
    return _run(args, prompt, cwd, timeout)

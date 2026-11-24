"""AGENT 6 — Voice & Sound (етап `voice`, audio-first: до розкадровки).

1. Кастинг: кожен голос із voice/requests.json має бути у voice/voices.json (лише вже використані студією голоси).
   Новий голос → BlockedError (кастинг — рішення власника).
2. Квота ElevenLabs: залишок знаків тарифу (API /v1/user/subscription) ≥ потрібного; інакше — людина.
3. Генерація — наявний scripts/audio/elevenlabs_episode.py (уже згенероване не генерується повторно, дублів не робимо).
4. Монтаж голосу — scripts/voice_build.py (темп 140 сл/хв, паузи з voice-plan.json).
Звукові ефекти й музика — на етапі edit (наявні процедурні SFX студії).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys

from ..core import budget, clock
from . import common
from .base import Agent, AgentResult, BlockedError, Context, RetryableError, register


def _load(root, rel, name):
    spec = importlib.util.spec_from_file_location(name, root / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str((root / rel).parent))
    spec.loader.exec_module(mod)
    return mod


def _blocks(ep) -> list[str]:
    md = (ep / "voice" / "elevenlabs.md").read_text(encoding="utf-8")
    return re.findall(r"^```\n(.*?)^```", md, flags=re.S | re.M)


def _sha(text: str, voice_id: str) -> str:
    return hashlib.sha1(f"{voice_id}\n{text}".encode("utf-8")).hexdigest()


def retire_stale(ep, stamp: str) -> list[str]:
    """Сценарій змінили (напр. виправлення після QC) → старе аудіо блоку не підходить. Файли не видаляються:
    переносяться в audio/raw/old-<час>/. Блоки без мітки .src.sha1 (епізоди власника до Studio OS) не чіпаємо."""
    raw = ep / "audio" / "raw"
    if not raw.exists():
        return []
    reqs = json.loads((ep / "voice" / "requests.json").read_text(encoding="utf-8"))["requests"]
    voices = json.loads((ep / "voice" / "voices.json").read_text(encoding="utf-8"))
    moved = []
    for r, b in zip(reqs, _blocks(ep)):
        mark = raw / f"{r['name']}.src.sha1"
        if not mark.exists() or mark.read_text().strip() == _sha(b, voices.get(r["voice"], {}).get("voice_id", "")):
            continue
        dst = raw / f"old-{stamp}"
        dst.mkdir(exist_ok=True)
        for f in list(raw.glob(f"{r['name']}.*")) + list(raw.glob(f"{r['name']}-*")):
            shutil.move(str(f), str(dst / f.name))
        moved.append(r["name"])
    return moved


def mark_sources(ep) -> None:
    raw = ep / "audio" / "raw"
    reqs = json.loads((ep / "voice" / "requests.json").read_text(encoding="utf-8"))["requests"]
    voices = json.loads((ep / "voice" / "voices.json").read_text(encoding="utf-8"))
    for r, b in zip(reqs, _blocks(ep)):
        if (raw / f"{r['name']}.json").exists():
            (raw / f"{r['name']}.src.sha1").write_text(_sha(b, voices.get(r["voice"], {}).get("voice_id", "")))


def chars_needed(ep) -> int:
    blocks = _blocks(ep)
    done = {p.stem for p in (ep / "audio" / "raw").glob("*.json")} if (ep / "audio" / "raw").exists() else set()
    reqs = json.loads((ep / "voice" / "requests.json").read_text(encoding="utf-8"))["requests"]
    return sum(len(b) for r, b in zip(reqs, blocks) if r["name"] not in done)


@register
class VoiceAgent(Agent):
    name = "voice"
    stage = "voice"
    resources = ("elevenlabs",)
    max_attempts = 2

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        cfg, root = ctx.cfg, ctx.cfg.root
        ep = common.episode_dir(ctx.con, cfg, ctx.video_id)
        if not ep or not (ep / "voice" / "requests.json").exists():
            raise BlockedError("немає voice/requests.json — спершу етап script")
        reqs = json.loads((ep / "voice" / "requests.json").read_text(encoding="utf-8"))["requests"]
        voices = json.loads((ep / "voice" / "voices.json").read_text(encoding="utf-8"))
        missing = sorted({r["voice"] for r in reqs} - set(voices))
        if missing:
            raise BlockedError(f"потрібен кастинг голосів: {', '.join(missing)} — додайте їх у {ep.name}/voice/voices.json")
        nk, nv = common.narrator(cfg)
        if nk in voices and voices[nk].get("voice_id") != nv["voice_id"]:
            raise BlockedError(f"голос диктора {nk} у voices.json — «{voices[nk].get('name')}», а має бути «{nv['name']}» "
                               f"({nv['voice_id']}) — виправте voices.json")
        if cfg.production.get("narrator_only", True) and {r["voice"] for r in reqs} - {nk}:
            raise BlockedError(f"увімкнено narrator_only, але в requests.json є інші голоси: "
                               f"{sorted({r['voice'] for r in reqs} - {nk})}")
        moved = retire_stale(ep, clock.now_iso().replace(":", "").replace("-", "")[:15])
        if moved:
            ctx.log("info", f"текст змінено — старе аудіо блоків {moved} перенесено в audio/raw/old-*/ (перегенерація)")
        need = chars_needed(ep)
        if need:
            el = _load(root, "scripts/audio/elevenlabs.py", "studio_elevenlabs")
            try:
                sub = el.api("/v1/user/subscription")
            except SystemExit as e:
                raise BlockedError(f"ElevenLabs: {e}") from e
            except Exception as e:  # noqa: BLE001 — мережа
                raise RetryableError(f"ElevenLabs недоступний: {e}") from e
            left = int(sub["character_limit"]) - int(sub["character_count"])
            ctx.log("info", f"ElevenLabs: потрібно ≈{need} знаків, залишок {left}")
            if left < need:
                raise BlockedError(f"знаків ElevenLabs недостатньо: потрібно {need}, залишок {left}")
            if not budget.allowed(ctx.con, cfg, "elevenlabs_chars", need):
                raise BlockedError("місячний бюджет знаків ElevenLabs у config вичерпано")
            p = subprocess.run([sys.executable, "-X", "utf8", str(root / "scripts/audio/elevenlabs_episode.py"), ep.name],
                               cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=3600)
            budget.spend(ctx.con, cfg, "elevenlabs_chars", need, video_id=ctx.video_id)
            if p.returncode != 0:
                raise RetryableError(f"elevenlabs_episode.py: {(p.stderr or p.stdout)[-800:]}")
            mark_sources(ep)
        ctx.heartbeat()
        vb = _load(root, "scripts/voice_build.py", "studio_voice_build")
        try:
            r = vb.build(ep, ep / "audio", float(cfg.production.get("words_per_minute", 140)))
        except SystemExit as e:
            raise BlockedError(f"voice_build: {e}") from e
        t = r["tempo"]
        rel = ep.relative_to(root).as_posix()
        wpm = float(cfg.production.get("words_per_minute", 140))
        warn = ""
        if abs(float(t["wpm_speech"]) - wpm) > wpm * 0.1:   # voice_build лише пришвидшує: природно швидший голос лишається швидшим
            warn = f" ⚠ темп {t['wpm_speech']} сл/хв відрізняється від цілі {wpm:g} більш ніж на 10 %"
            ctx.log("warning", warn.strip())
        return AgentResult(summary=f"голос: {len(r['lines'])} реплік, {t['voice_s']} с, {t['wpm_speech']} сл/хв (знаків витрачено ≈{need}){warn}",
                           data={"tempo": t, "chars": need},
                           artifacts=[f"{rel}/audio/voice.wav", f"{rel}/audio/voice-lines.json", f"{rel}/audio/tempo.json"])

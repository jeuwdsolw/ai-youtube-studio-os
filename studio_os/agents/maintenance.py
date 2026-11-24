"""Службові задачі.

cleanup_frames — видалення PNG-послідовностей Blender (`episodes/<ep>/renders/<shot>/f_####.png`) ПІСЛЯ кодування
(дозвіл власника 2026-11-23). Видаляються лише кадри, для яких є закодоване відео `<public_dir>/<shot>.mp4`
(з `episodes/<ep>/pipeline.json`), новіше за кадри і з кількістю кадрів ≥ min_match × кількість PNG (перевірка ffprobe).
Інші файли теки (перевірочні кадри, mp4, json) не чіпаються. За замовчуванням — dry run.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .base import Agent, AgentResult, BlockedError, Context, register

FRAME_RE = re.compile(r"^f_\d{4,}\.png$")


def find_ffprobe() -> str | None:
    p = shutil.which("ffprobe")
    if p:
        return p
    base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    hits = sorted(base.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffprobe.exe")) if base.exists() else []
    return str(hits[-1]) if hits else None


def video_frames(ffprobe: str, path: Path) -> int | None:
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout.strip()
        return int(out.split(",")[0])
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


@dataclass
class Candidate:
    frames_dir: Path
    pngs: list[Path]
    video: Path | None
    video_frames: int | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.reason == "ok"

    @property
    def bytes(self) -> int:
        return sum(p.stat().st_size for p in self.pngs)


def scan(root: Path, ffprobe: str | None, min_match: float = 0.98) -> list[Candidate]:
    out = []
    for ep in sorted((root / "episodes").glob("*/")):
        renders = ep / "renders"
        if not renders.is_dir():
            continue
        public_dir = None
        pj = ep / "pipeline.json"
        if pj.exists():
            try:
                pd = json.loads(pj.read_text(encoding="utf-8")).get("public_dir")
                public_dir = root / pd if pd else None
            except (json.JSONDecodeError, OSError):
                public_dir = None
        for shot in sorted(p for p in renders.iterdir() if p.is_dir()):
            pngs = sorted(p for p in shot.iterdir() if p.is_file() and FRAME_RE.match(p.name))
            if not pngs:
                continue
            video = public_dir / f"{shot.name}.mp4" if public_dir else None
            if video is None or not video.exists() or video.stat().st_size == 0:
                out.append(Candidate(shot, pngs, video, None, "немає закодованого відео — не видаляю"))
                continue
            if video.stat().st_mtime < max(p.stat().st_mtime for p in pngs):
                out.append(Candidate(shot, pngs, video, None, "відео старіше за кадри (перекодуй) — не видаляю"))
                continue
            if not ffprobe:
                out.append(Candidate(shot, pngs, video, None, "ffprobe не знайдено — не можу перевірити"))
                continue
            n = video_frames(ffprobe, video)
            if n is None or n < min_match * len(pngs):
                out.append(Candidate(shot, pngs, video, n, f"у відео {n} кадрів з {len(pngs)} PNG — не видаляю"))
                continue
            out.append(Candidate(shot, pngs, video, n, "ok"))
    return out


@register
class CleanupFrames(Agent):
    name = "maintenance.cleanup_frames"
    stage = None
    max_attempts = 1

    def run(self, ctx: Context, payload: dict) -> AgentResult:
        if not ctx.cfg.maintenance.get("cleanup_frames", False):
            raise BlockedError("maintenance.cleanup_frames вимкнено в config/studio.toml")
        dry = bool(payload.get("dry_run", True))
        cands = scan(ctx.cfg.root, find_ffprobe(), float(ctx.cfg.maintenance.get("cleanup_frames_min_match", 0.98)))
        freed, removed, report = 0, 0, []
        for c in cands:
            rel = c.frames_dir.relative_to(ctx.cfg.root).as_posix()
            report.append({"dir": rel, "pngs": len(c.pngs), "mb": round(c.bytes / 1e6, 1),
                           "video": c.video.relative_to(ctx.cfg.root).as_posix() if c.video else None,
                           "video_frames": c.video_frames, "decision": c.reason})
            if c.ok and not dry:
                size = c.bytes
                for p in c.pngs:
                    p.unlink()
                freed += size
                removed += len(c.pngs)
                ctx.log("info", f"видалено {len(c.pngs)} PNG ({size / 1e6:.0f} МБ) у {rel}")
            if ctx.should_stop():
                break
        ok_mb = sum(r["mb"] for r, c in zip(report, cands) if c.ok)
        summary = (f"dry run: можна звільнити {ok_mb:.0f} МБ у {sum(c.ok for c in cands)} теках"
                   if dry else f"видалено {removed} PNG, звільнено {freed / 1e6:.0f} МБ")
        return AgentResult(summary=summary, data={"dry_run": dry, "dirs": report})

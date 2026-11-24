"""Вимірювання готового відео (ffmpeg/ffprobe) для QC. Лише читання файлів, нічого не змінює.

Основа — наявна перевірка студії `tests/check_video.py` (формат, рух по секундах, гучність, контрольні кадри);
поверх неї: чорні кадри (blackdetect), застиглі відрізки (freezedetect), тиша (silencedetect).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def tool(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    hits = sorted(base.glob(f"Gyan.FFmpeg*/ffmpeg-*/bin/{name}.exe")) if base.exists() else []
    return str(hits[-1]) if hits else None


def _ff(args: list[str], timeout: int = 1800) -> str:
    ff = tool("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg не знайдено")
    p = subprocess.run([ff, "-hide_banner", "-nostats", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.stdout + p.stderr


@dataclass
class Probe:
    width: int = 0
    height: int = 0
    fps: str = ""
    duration: float = 0.0
    audio: int = 0


def probe(video: Path) -> Probe:
    fp = tool("ffprobe")
    if not fp:
        raise RuntimeError("ffprobe не знайдено")
    d = json.loads(subprocess.run([fp, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
                                  capture_output=True, text=True, encoding="utf-8").stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    return Probe(width=int(v.get("width", 0)), height=int(v.get("height", 0)), fps=v.get("r_frame_rate", ""),
                 duration=float(d.get("format", {}).get("duration", 0) or 0),
                 audio=sum(1 for s in d.get("streams", []) if s.get("codec_type") == "audio"))


@dataclass
class CheckVideo:
    """Результат наявного tests/check_video.py (розбір його виводу)."""
    passed: bool
    lines: list[tuple[bool, str]] = field(default_factory=list)
    per_sec: list[float] = field(default_factory=list)
    static_secs: list[int] = field(default_factory=list)
    lufs: float | None = None
    true_peak: float | None = None
    max_volume: float | None = None
    frames_dir: Path | None = None
    raw: str = ""


def check_video(root: Path, video: Path, timeout: int = 1800) -> CheckVideo:
    p = subprocess.run([sys.executable, "-X", "utf8", str(root / "tests/check_video.py"), str(video)], cwd=root,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    out = p.stdout + p.stderr
    r = CheckVideo(passed=p.returncode == 0 and "РЕЗУЛЬТАТ: PASS" in out, raw=out[-4000:])
    for ln in out.splitlines():
        s = ln.strip()
        if s.startswith(("✓", "✗")):
            r.lines.append((s.startswith("✓"), s[1:].strip()))
        if "рух по секундах" in s:
            r.per_sec = [float(x) for x in re.findall(r"-?\d+\.\d+", s.split(":", 1)[1])]
        if m := re.search(r"нерухомих секунд: \d+ \[([\d, ]*)\]", s):
            r.static_secs = [int(x) for x in m.group(1).replace(" ", "").split(",") if x]
        if m := re.search(r"гучність (-?[\d.]+) LUFS", s):
            r.lufs = float(m.group(1))
        if m := re.search(r"true peak (-?[\d.]+) dBTP", s):
            r.true_peak = float(m.group(1))
        if m := re.search(r"пік (-?[\d.]+) dB", s):
            r.max_volume = float(m.group(1))
        if s.startswith("контрольні кадри:"):
            r.frames_dir = Path(s.split(":", 1)[1].strip())
    return r


def _segments(text: str, start_key: str, end_key: str) -> list[tuple[float, float]]:
    starts = [float(x) for x in re.findall(rf"{start_key}:\s*(-?[\d.]+)", text)]
    ends = [float(x) for x in re.findall(rf"{end_key}:\s*(-?[\d.]+)", text)]
    return list(zip(starts, ends + [float("nan")] * (len(starts) - len(ends))))


def black_segments(video: Path, min_s: float = 0.5, pix_th: float = 0.03) -> list[tuple[float, float]]:
    """pix_th 0.03 — справжній чорний екран; 0.10 — «майже чорний» (дуже темний план, напр. нічна кімната)."""
    out = _ff(["-i", str(video), "-vf", f"scale=320:-2,blackdetect=d={min_s}:pix_th={pix_th}", "-an", "-f", "null", "-"])
    return _segments(out, "black_start", "black_end")


def freeze_segments(video: Path, min_s: float = 2.0) -> list[tuple[float, float]]:
    out = _ff(["-i", str(video), "-vf", f"scale=320:-2,freezedetect=n=0.0008:d={min_s}", "-an", "-f", "null", "-"])
    return _segments(out, "lavfi.freezedetect.freeze_start", "lavfi.freezedetect.freeze_end")


def silence_segments(video: Path, min_s: float = 1.5, noise_db: float = -50) -> list[tuple[float, float]]:
    out = _ff(["-i", str(video), "-af", f"silencedetect=n={noise_db}dB:d={min_s}", "-vn", "-f", "null", "-"])
    return _segments(out, "silence_start", "silence_end")


def inner(segs: list[tuple[float, float]], duration: float, head: float, tail: float) -> list[tuple[float, float]]:
    """Відрізки, що не є навмисним початком/кінцем (затемнення на вході й фінальна склейка)."""
    out = []
    for a, b in segs:
        b = duration if b != b else b  # NaN → до кінця
        if b <= head or a >= duration - tail:
            continue
        out.append((round(a, 2), round(b, 2)))
    return out

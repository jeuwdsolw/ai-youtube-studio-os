"""Конвеєр епізоду: build → render → encode → audio → video → check.

    py -3.12 scripts/pipeline.py episode-001                    # усе
    py -3.12 scripts/pipeline.py episode-001 --steps video,check # лише перезібрати відео (після змін тексту/таймінгів)
    py -3.12 scripts/pipeline.py episode-001 --steps render --shots shot04 --res 50 --samples 16  # чернетка плану

Налаштування епізоду: episodes/<ep>/pipeline.json. Шляхи до програм: BLENDER_PATH, FFMPEG_PATH (або автопошук).
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

# Консоль Windows (cp1251) не друкує ✓ — при виводі в pipe/файл.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

STUDIO = Path(__file__).resolve().parents[1]
STEPS = ["build", "render", "encode", "audio", "video", "check"]


def find_blender() -> str:
    p = os.environ.get("BLENDER_PATH", r"C:\Games\steamapps\common\Blender\blender.exe")
    if not Path(p).is_file():
        sys.exit(f"Blender не знайдено: {p} (задайте BLENDER_PATH)")
    return p


def find_ffmpeg(name="ffmpeg") -> str:
    env = os.environ.get("FFMPEG_PATH")
    if env:
        return str(Path(env).with_name(name + ".exe")) if name != "ffmpeg" else env
    found = shutil.which(name)
    if found:
        return found
    hits = glob.glob(os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin\{name}.exe"))
    if hits:
        return hits[0]
    sys.exit(f"{name} не знайдено (winget install Gyan.FFmpeg або FFMPEG_PATH)")


def run(cmd, cwd=STUDIO, quiet=True):
    t = time.time()
    print("  $", " ".join(str(c) for c in cmd)[:200])
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=quiet, text=True, encoding="utf-8",
                       errors="replace", shell=False)
    if r.returncode != 0:
        if quiet:
            print((r.stdout or "")[-3000:], (r.stderr or "")[-3000:])
        sys.exit(f"Команда впала з кодом {r.returncode}")
    out = (r.stdout or "") + (r.stderr or "") if quiet else ""
    if "Traceback" in out or "Error:" in out:
        print(out[-3000:])
        sys.exit("Blender/Python повідомив про помилку (див. вище)")
    print(f"    ✓ {time.time() - t:.1f} с")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--shots")
    ap.add_argument("--res", type=int)
    ap.add_argument("--samples", type=int)
    a = ap.parse_args()

    ep = STUDIO / "episodes" / a.episode
    cfg = json.loads((ep / "pipeline.json").read_text(encoding="utf-8"))
    steps = a.steps.split(",")
    shots = a.shots.split(",") if a.shots else cfg["shots"]
    res = a.res or cfg["render"]["res"]
    samples = a.samples or cfg["render"]["samples"]
    blender = find_blender()
    public = STUDIO / cfg["public_dir"]

    if "build" in steps:
        print("[build] збирання сцени Blender")
        run([blender, "--background", "--factory-startup", "-noaudio", "--python", STUDIO / cfg["build_script"]])

    if "render" in steps:
        for s in shots:
            print(f"[render] {s} ({res}%, {samples} семплів)")
            run([blender, "-b", "-noaudio", STUDIO / cfg["blend"], "--python", STUDIO / "scripts/blender/render_shot.py",
                 "--", "--shot", s, "--res", res, "--samples", samples])

    if "encode" in steps:
        ff = find_ffmpeg()
        public.mkdir(parents=True, exist_ok=True)
        for s in shots:
            print(f"[encode] {s} → {public.name}/{s}.mp4")
            run([ff, "-y", "-loglevel", "error", "-framerate", "30", "-i", ep / "renders" / s / "f_%04d.png",
                 "-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p", public / f"{s}.mp4"])

    if "audio" in steps:
        print("[audio] генерація звукових ефектів")
        run([sys.executable, STUDIO / "scripts/audio/gen_sfx.py", a.episode])

    if "video" in steps:
        print("[video] рендер Remotion")
        out = STUDIO / cfg["output"]
        out.parent.mkdir(parents=True, exist_ok=True)
        npx = shutil.which("npx.cmd") or shutil.which("npx")
        run([npx, "remotion", "render", "src/index.ts", cfg["composition"], out, "--log=error"], cwd=STUDIO / "video")

    if "check" in steps:
        print("[check] перевірка результату")
        run([sys.executable, STUDIO / "tests/check_video.py", STUDIO / cfg["output"]], quiet=False)


if __name__ == "__main__":
    main()

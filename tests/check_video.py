"""Перевірка готового відео: формат, звук, рух у кожному плані, контрольні кадри.

    py -3.12 tests/check_video.py episodes/episode-001/renders/ep001_final.mp4

Рух: середня різниця між сусідніми кадрами в кожному вікні 1 с має бути > порогу
(доказ, що відео не «слайд-шоу»). Контрольні кадри пишуться в <video>_check/ для перегляду.
"""

import re
import glob
import json
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path


def tool(name):
    found = shutil.which(name)
    if found:
        return found
    hits = glob.glob(os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin\{name}.exe"))
    if not hits:
        sys.exit(f"{name} не знайдено")
    return hits[0]


def main():
    video = Path(sys.argv[1])
    # --still A-B: навмисно нерухомі секунди за сценарієм (напр. повністю чорний кадр еп. 008, с 79–82) — не рахуються, але друкуються
    allowed = set()
    if "--still" in sys.argv:
        a, b = sys.argv[sys.argv.index("--still") + 1].split("-")
        allowed = set(range(int(a), int(b) + 1))
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok &= bool(cond)

    check(video.is_file(), f"файл існує: {video}")
    if not video.is_file():
        sys.exit(1)
    probe = json.loads(subprocess.run([tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
                                      capture_output=True, text=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    a = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    dur = float(probe["format"]["duration"])
    # 16:9 (звичайні епізоди) або 9:16 (Shorts)
    check((v["width"], v["height"]) in ((1920, 1080), (1080, 1920)), f"роздільність {v['width']}×{v['height']}")
    check(v["r_frame_rate"] in ("30/1", "30000/1000"), f"fps {v['r_frame_rate']}")
    check(dur > 5, f"тривалість {dur:.2f} с")
    check(len(a) == 1, f"аудіодоріжка: {a[0]['codec_name'] if a else 'немає'}")

    # Рух: різниця сусідніх кадрів (ffmpeg signalstats YDIF) усереднена по секундах.
    r = subprocess.run([tool("ffmpeg"), "-v", "info", "-i", str(video), "-vf", "scale=320:-1,signalstats,metadata=print:key=lavfi.signalstats.YDIF",
                        "-f", "null", "-"], capture_output=True, text=True)
    ydif = [float(line.split("=")[1]) for line in (r.stdout + r.stderr).splitlines() if "YDIF=" in line]
    per_sec = [sum(ydif[i:i + 30]) / max(1, len(ydif[i:i + 30])) for i in range(0, len(ydif), 30)]
    static = [i for i, s in enumerate(per_sec) if s < 0.05]
    print("    рух по секундах (YDIF):", " ".join(f"{s:.2f}" for s in per_sec))
    if allowed & set(static):
        print(f"    навмисно нерухомі (--still): {sorted(allowed & set(static))}")
    static = [i for i in static if i not in allowed]
    check(len(ydif) > 0 and len(static) <= 1, f"рух у кожній секунді (нерухомих секунд: {len(static)} {static})")

    # Гучність: не тиша і без кліпінгу.
    r = subprocess.run([tool("ffmpeg"), "-v", "info", "-i", str(video), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True)
    vol = {k: float(line.split(":")[1].split()[0]) for line in r.stderr.splitlines() for k in ("mean_volume", "max_volume") if k in line}
    check(vol.get("mean_volume", -99) > -45, f"середня гучність {vol.get('mean_volume')} dB")
    check(vol.get("max_volume", 0) <= -0.1, f"пік {vol.get('max_volume')} dB (без кліпінгу)")
    # Гучність під YouTube (EBU R128): інтегральна −14 ± 1.5 LUFS, true peak ≤ −1 dBTP (з допуском 0.1 на вимір).
    r = subprocess.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video), "-af", "ebur128=peak=true", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    lufs = [float(x) for x in re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)]
    tp = [float(x) for x in re.findall(r"Peak:\s+(-?[\d.]+) dBFS", r.stderr)]
    if lufs and tp:
        check(-15.5 <= lufs[-1] <= -12.5, f"гучність {lufs[-1]} LUFS (YouTube: −14 ± 1.5; мастер — scripts/audio/master.py)")
        check(tp[-1] <= -0.9, f"true peak {tp[-1]} dBTP (≤ −1)")

    out = video.with_name(video.stem + "_check")
    out.mkdir(exist_ok=True)
    for t in (1.0, 2.6, 5.5, 8.4, 9.6, 11.2, 13.0, 14.6, 17.5):
        if t < dur:
            subprocess.run([tool("ffmpeg"), "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                            str(out / f"t{t:05.1f}.png")])
    print("    контрольні кадри:", out)
    print("РЕЗУЛЬТАТ:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

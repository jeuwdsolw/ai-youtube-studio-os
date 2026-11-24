"""Звіт міксу: чи не перекрикують ефекти голос.

    py -3.12 -X utf8 scripts/audio/mix_report.py <episode-id> <voice.wav> <fx.wav> [--max-delta -6]

Стеми рендеряться з композиції епізоду з пропом stem (див. EpXXX.tsx):
    npx remotion render Ep002 voice.wav --codec=wav --props=voice.json   # {"stem":"voice"}
    npx remotion render Ep002 fx.wav    --codec=wav --props=fx.json      # {"stem":"fx"}

Для кожного ефекту з edit.json: пік гучності ефекту (momentary LUFS, вікно 400 мс, K-зважування — як чує вухо)
проти гучності голосу в тому самому місці. Якщо голосу поруч немає — порівнюємо з типовою гучністю мови.
Ефект під голосом має бути тихіший за short-term гучність мови щонайменше на |max-delta| LU.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from statistics import median

STUDIO = Path(__file__).resolve().parents[2]


def ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for p in (Path.home() / "AppData/Local/Microsoft/WinGet/Packages").glob("*FFmpeg*/**/ffmpeg.exe"):
        return str(p)
    sys.exit("ffmpeg не знайдено")


def momentary(wav, key="M"):
    """Momentary (M, вікно 400 мс) або short-term (S, 3 с) loudness у LUFS кожні 100 мс."""
    out = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-v", "verbose", "-i", str(wav), "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    vals = []
    for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+|-inf)\s+S:\s*(-?[\d.]+|-inf)", out):
        g = m.group(2 if key == "M" else 3)
        vals.append((float(m.group(1)), -120.0 if g == "-inf" else float(g)))
    return vals


def window(series, a, b):
    return [v for t, v in series if a <= t <= b]


def main():
    args = sys.argv[1:]
    max_delta = -6.0
    if "--max-delta" in args:
        i = args.index("--max-delta")
        max_delta = float(args[i + 1])
        del args[i : i + 2]
    ep, voice_wav, fx_wav = args
    edit = json.loads((STUDIO / "episodes" / ep / "edit.json").read_text(encoding="utf-8"))
    fps = edit["fps"]
    starts, t = {}, 0
    for s in edit["timeline"]:
        starts[s["id"]] = t
        t += s["frames"]

    V, F = momentary(voice_wav), momentary(fx_wav)
    VS = dict(momentary(voice_wav, "S"))  # сприйнята гучність мови — short-term
    speech = [v for _, v in V if v > -40]
    typical = median(speech) if speech else -20.0
    print(f"голос: типова гучність мови {typical:.1f} LUFS (momentary, медіана)")
    print(f"{'ефект':<22}{'час':>7}{'ефект':>9}{'голос':>9}{'різниця':>9}")
    bad = 0
    Vd = dict(V)
    for x in edit["sfx"]:
        t0 = (starts[x["shot"]] + x["frame"]) / fps
        fx_peak = max(window(F, t0, t0 + 1.2) or [-120])
        # лише там, де голос звучить (momentary не тихіше медіани мови − 10 LU); еталон — short-term гучність мови (3 с),
        # бо вухо оцінює фразу, а не провали між складами. У паузах ефект і має бути чутно.
        # На початку фрази після паузи short-term (вікно 3 с) ще «відстає» від мови → еталон = max(short-term, momentary).
        pairs = [(f, max(VS.get(t, -120), Vd[t])) for t, f in F if t0 <= t <= t0 + 1.2 and Vd.get(t, -120) > typical - 10]
        near = bool(pairs)
        if near:
            fx_at, ref = max(pairs, key=lambda p: p[0] - p[1])
            d = fx_at - ref
        else:
            ref, d = typical, fx_peak - typical
        ok = d <= max_delta if near else d <= 0
        bad += not ok
        print(f"{x['src'].replace('sfx/', ''):<22}{t0:>6.1f}s{fx_peak:>9.1f}{(ref if near else float('nan')):>9.1f}{d:>+9.1f}  {'OK' if ok else 'ЗАГУЧНО'}")
    beds = [v for t_, v in F if v > -70]
    print(f"фон (ефекти+фони) медіана: {median(beds):.1f} LUFS → на {typical - median(beds):.1f} LU тихіше за мову")
    print("РЕЗУЛЬТАТ:", "PASS" if bad == 0 else f"FAIL ({bad})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

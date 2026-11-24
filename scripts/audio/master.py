"""Мастеринг звуку готового відео під YouTube: вимір (loudnorm) → лінійне підсилення + лімітер → −14 LUFS інтегрально, true peak ≤ −1 dBTP.
Відео копіюється без перекодування; оригінал не змінюється (пише <name>_master.mp4).

    py -3.12 -X utf8 scripts/audio/master.py episodes/episode-003/renders/ep003_final.mp4 [--lufs -14] [--tp -1]

Мікс (голос над ефектами) перевіряється ДО мастерингу — scripts/audio/mix_report.py; мастеринг
лише зсуває гучність (лімітер — тільки на піках), не змінюючи співвідношення голосу й ефектів.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mix_report import ffmpeg  # noqa: E402


def main():
    args = sys.argv[1:]
    opts = {"--lufs": -14.0, "--tp": -1.0}
    for k in list(opts):
        if k in args:
            i = args.index(k)
            opts[k] = float(args[i + 1])
            del args[i : i + 2]
    src = Path(args[0])
    out = src.with_name(src.stem + "_master.mp4")
    I, TP = opts["--lufs"], opts["--tp"]
    ln = f"loudnorm=I={I}:TP={TP}:LRA=11"
    r = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(src), "-af", ln + ":print_format=json", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    m = json.loads(re.findall(r"\{[^{}]*\}", r)[-1])
    print(f"вхід: {m['input_i']} LUFS, TP {m['input_tp']} dBTP, LRA {m['input_lra']}")
    # Лінійне підсилення до цілі + пікoвий лімітер (4× передискретизація ≈ true peak) із запасом 1 дБ під AAC.
    # Лімітер чіпає лише рідкісні піки голосу — співвідношення голос/ефекти лишається як у міксі.
    gain = I - float(m["input_i"])
    ceil = 10 ** ((TP - 1.0) / 20)
    for attempt in range(3):
        af = f"volume={gain:.2f}dB,aresample=192000,alimiter=limit={ceil:.4f}:attack=3:release=60:level=disabled,aresample=48000"
        subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "192k", str(out)], check=True)
        r = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(out), "-af", "ebur128=peak=true", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
        i_out = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r)[-1])
        tp_out = float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", r)[-1])
        print(f"вихід: {i_out} LUFS, true peak {tp_out} dBTP")
        if tp_out <= TP and abs(i_out - I) <= 0.5:
            break
        gain += I - i_out
        ceil *= 10 ** (min(0, TP - tp_out - 0.2) / 20)
    print(f"→ {out}")


if __name__ == "__main__":
    main()

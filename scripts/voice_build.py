"""Загальний монтаж голосу епізоду (узагальнення scripts/eNN_voice_build.py; механіка — як e17_voice_build.py).

    py -3.12 -X utf8 scripts/voice_build.py <episode> [--wpm 140] [--out <тека замість audio/>]

Джерела (НЕ змінюються): episodes/<ep>/audio/lines/<ID>.mp3, voice/elevenlabs.md + voice/requests.json (тексти),
voice/voice-plan.json — {"pause_in", "gap", "filters": {NAME: ffmpeg-af}, "order": [{"id", "filter", "pre"}]}.
Паузи всередині реплік ≤ pause_in, між репліками gap, atempo так, щоб слова / (мова + gap) = wpm (не повільніше оригіналу).
Паузи-дії (`pre`) — поза темпом мови. Результат: <out>/proc/<ID>.wav, voice.wav, tempo.json, voice-lines.json.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
STUDIO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDIO / "scripts"))
from pipeline import find_ffmpeg  # noqa: E402

FF = find_ffmpeg()


def load_texts(ep: Path) -> dict:
    md = (ep / "voice" / "elevenlabs.md").read_text(encoding="utf-8")
    blocks = re.findall(r"^```\n(.*?)^```", md, flags=re.S | re.M)
    reqs = json.loads((ep / "voice" / "requests.json").read_text(encoding="utf-8"))["requests"]
    texts = {}
    for req, blk in zip(reqs, blocks):
        paras = [p for p in blk.strip().split("\n\n") if p.strip()]
        for lid, p in zip(req["ids"], paras):
            texts[lid] = re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", p)).strip()
    return texts


def words(t):
    return len(re.findall(r"[\w'’-]+", t))


def dur(p):
    return float(subprocess.check_output([find_ffmpeg("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                                          "-of", "csv=p=0", str(p)]))


def run(args):
    subprocess.run([FF, "-v", "error", "-y", *args], check=True)


def build(ep: Path, out: Path, wpm: float) -> dict:
    plan = json.loads((ep / "voice" / "voice-plan.json").read_text(encoding="utf-8"))
    pause_in, gap_s, filters = plan.get("pause_in", 0.18), plan.get("gap", 0.30), plan["filters"]
    order = [(o["id"], filters[o["filter"]], float(o.get("pre", 0))) for o in plan["order"]]
    texts = load_texts(ep)
    missing = [lid for lid, _, _ in order if lid not in texts or not (ep / "audio" / "lines" / f"{lid}.mp3").exists()]
    if missing:
        raise SystemExit(f"немає тексту або аудіо для реплік: {', '.join(missing)}")
    proc = out / "proc"
    proc.mkdir(parents=True, exist_ok=True)
    sil = (f"silenceremove=start_periods=1:start_threshold=-45dB:stop_periods=-1:stop_duration={pause_in}"
           f":stop_threshold=-45dB:stop_silence={pause_in},areverse,"
           "silenceremove=start_periods=1:start_threshold=-45dB,areverse")
    tight = {}
    for lid, _, _ in order:
        o = proc / f"{lid}.tight.wav"
        run(["-i", str(ep / "audio" / "lines" / f"{lid}.mp3"), "-af", sil, "-ar", "44100", "-ac", "1", str(o)])
        tight[lid] = dur(o)
    n = sum(words(texts[lid]) for lid, _, _ in order)
    gaps = gap_s * (len(order) - 1)
    target = n / wpm * 60
    tempo = max(1.0, (sum(tight.values()) + gaps) / target)
    print(f"слів {n}, мова {sum(tight.values()):.2f} с + проміжки {gaps:.2f} с, ціль {target:.2f} с → atempo ×{tempo:.3f}")
    lines, t, parts = [], 0.0, []
    for k, (lid, flt, pre) in enumerate(order):
        o = proc / f"{lid}.wav"
        run(["-i", str(proc / f"{lid}.tight.wav"), "-af", f"atempo={tempo:.4f},{flt},loudnorm=I=-20:TP=-2:LRA=11",
             "-ar", "44100", "-ac", "1", str(o)])
        gap = (pre + (gap_s if k else 0.0)) / tempo
        if gap > 0:
            g = proc / f"gap{k:02d}.wav"
            run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", f"{gap:.4f}", str(g)])
            parts.append(g.name)
            t += gap
        d = dur(o)
        lines.append({"id": lid, "start": round(t, 3), "end": round(t + d, 3), "text": texts[lid]})
        parts.append(o.name)
        t += d
    lst = proc / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    run(["-f", "concat", "-safe", "0", "-i", str(lst), "-ar", "44100", str(out / "voice.wav")])
    real = dur(out / "voice.wav")
    action = sum(p for _, _, p in order) / tempo
    res = {"words": n, "voice_s": round(real, 2), "wpm_speech": round(n / (real - action) * 60, 1),
           "wpm_total": round(n / real * 60, 1), "action_pauses_s": round(action, 2), "tempo": round(tempo, 3),
           "pause_in": pause_in, "gap_s": round(gap_s / tempo, 3), "target_wpm": wpm}
    (out / "tempo.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "voice-lines.json").write_text(json.dumps(lines, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"tempo": res, "lines": lines}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--wpm", type=float, default=140.0)
    ap.add_argument("--out", help="тека результату (за замовчуванням episodes/<ep>/audio)")
    a = ap.parse_args()
    ep = STUDIO / "episodes" / a.episode
    out = Path(a.out) if a.out else ep / "audio"
    r = build(ep, out, a.wpm)
    print(json.dumps(r["tempo"], ensure_ascii=False))
    for ln in r["lines"]:
        print(f"  {ln['id']} {ln['start']:6.2f}–{ln['end']:6.2f}  {ln['text']}")


if __name__ == "__main__":
    main()

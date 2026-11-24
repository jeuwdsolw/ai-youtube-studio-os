"""Озвучка епізоду в ElevenLabs за voice/elevenlabs.md: elevenlabs_episode.py <ep> [--only name,...] [--cut-only]

Один запит = один блок ``` з elevenlabs.md (порядок блоків = порядок у voice/requests.json), голоси — voice/voices.json.
Сирий запис і таймінги символів → audio/raw/<name>.mp3/.json; шматки по абзацах → audio/lines/<ID>.mp3.
Уже згенерований блок (є raw/<name>.json) повторно НЕ генерується — знаки не витрачаються; --cut-only лише ріже.
"""
import argparse
import base64
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import elevenlabs as el  # noqa: E402
from pipeline import find_ffmpeg  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PAD = 0.12  # с тиші навколо мови, коли сусідній шматок — без слів (подих DA-03)


def blocks(md: str):
    return [b.strip("\n") for b in re.findall(r"^```\n(.*?)^```", md, flags=re.S | re.M)]


def letters(text: str):
    """Літери поза [тегами]: [(індекс у тексті, літера)]."""
    out, depth = [], 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and ch.isalpha():
            out.append((i, ch.lower()))
    return out


def synth(text, voice_id, cfg):
    return el.api(f"/v1/text-to-speech/{voice_id}/with-timestamps", params={"output_format": "mp3_44100_192"},
                  body={"text": text, "model_id": cfg["model"], "language_code": cfg.get("language", "uk"),
                        "voice_settings": {"stability": cfg["stability"]}})


def generate(req, paras, voice_id, cfg, raw: Path):
    """Один запит; якщо блок задовгий для моделі — два (поділ split_at, як в інструкції). Повертає (mp3, alignment)."""
    try:
        parts = [synth("\n\n".join(paras), voice_id, cfg)]
    except RuntimeError as e:
        if "split_at" not in req or not re.search(r"length|limit|characters|too long|max", str(e), re.I):
            raise
        print(f"  {req['name']}: задовгий для одного запиту → 2 запити ({e})")
        groups = [paras[:req["split_at"]], paras[req["split_at"]:]]
        parts = [synth("\n\n".join(g), voice_id, cfg) for g in groups]
    if len(parts) == 1:
        p = parts[0]
        (raw / f"{req['name']}.mp3").write_bytes(base64.b64decode(p["audio_base64"]))
        a = p["alignment"]
        chars, starts, ends = a["characters"], a["character_start_times_seconds"], a["character_end_times_seconds"]
    else:
        chars, starts, ends, offset = [], [], [], 0.0
        for i, p in enumerate(parts):
            part = raw / f"{req['name']}.part{i}.mp3"
            part.write_bytes(base64.b64decode(p["audio_base64"]))
            a = p["alignment"]
            if i:  # межа частин = межа абзаців, як у суцільному тексті
                chars += ["\n", "\n"]; starts += [offset] * 2; ends += [offset] * 2
            chars += a["characters"]
            starts += [t + offset for t in a["character_start_times_seconds"]]
            ends += [t + offset for t in a["character_end_times_seconds"]]
            offset += duration_of(part)
        concat(raw, req["name"], len(parts))
    return {"text": "\n\n".join(paras), "characters": chars, "starts": starts, "ends": ends, "requests": len(parts)}


def duration_of(path: Path) -> float:
    ff = find_ffmpeg("ffprobe")
    return float(subprocess.check_output([ff, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]))


def concat(raw: Path, name: str, n: int):
    lst = raw / f"{name}.concat.txt"
    lst.write_text("".join(f"file '{name}.part{i}.mp3'\n" for i in range(n)), encoding="utf-8")
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                    str(raw / f"{name}.mp3")], check=True)


def spans(paras, align):
    """[початок, кінець мови] кожного абзацу в секундах (None — абзац без слів)."""
    al = letters("".join(align["characters"]))
    want, owner = [], []
    for k, p in enumerate(paras):
        for _, ch in letters(p):
            want.append(ch); owner.append(k)
    got = [ch for _, ch in al]
    first, last = {}, {}
    for blk in difflib.SequenceMatcher(None, want, got, autojunk=False).get_matching_blocks():
        for j in range(blk.size):
            k, idx = owner[blk.a + j], al[blk.b + j][0]
            first.setdefault(k, align["starts"][idx])
            last[k] = align["ends"][idx]
    return [(first[k], last[k]) if k in first else None for k in range(len(paras))]


def silences(mp3: Path):
    """Тиша у записі [(початок, кінець)] — таймінги символів ElevenLabs місцями зсунуті, межі прилипають до тиші."""
    log = subprocess.run([find_ffmpeg(), "-i", str(mp3), "-af", "silencedetect=noise=-40dB:d=0.2", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    st = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    en = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    return list(zip(st, en))


def snap(sil, lo, hi, key):
    """Найдовша тиша, у якої key(тиша) потрапляє в [lo, hi]."""
    hits = [s for s in sil if lo <= key(s) <= hi]
    return max(hits, key=lambda s: s[1] - s[0]) if hits else None


def cut_points(sp, total, sil):
    """Межі шматків: посередині паузи між абзацами; абзац без слів забирає всю паузу (± PAD)."""
    bounds = [0.0]
    for k in range(len(sp) - 1):
        a, b = sp[k], sp[k + 1]
        if a and b:
            t = (a[1] + b[0]) / 2
            s = snap(sil, t - 1.2, t + 1.2, lambda s: (s[0] + s[1]) / 2)
            bounds.append((s[0] + s[1]) / 2 if s else t)
        elif a:  # наступний без слів: межа — початок тиші після мови
            s = snap(sil, a[1] - 0.5, a[1] + 2.5, lambda s: s[0])
            bounds.append((s[0] if s else a[1]) + PAD)
        elif b:  # цей без слів: межа — кінець тиші перед мовою
            s = snap(sil, b[0] - 2.5, b[0] + 0.5, lambda s: s[1])
            bounds.append((s[1] if s else b[0]) - PAD)
        else:
            bounds.append(bounds[-1])
    return list(zip(bounds, bounds[1:] + [total]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ep")
    ap.add_argument("--only", default="")
    ap.add_argument("--cut-only", action="store_true")
    a = ap.parse_args()
    ep = ROOT / "episodes" / a.ep
    cfg = json.loads((ep / "voice" / "requests.json").read_text(encoding="utf-8"))
    voices = json.loads((ep / "voice" / "voices.json").read_text(encoding="utf-8"))
    md_blocks = blocks((ep / "voice" / cfg["source"]).read_text(encoding="utf-8"))
    if len(md_blocks) != len(cfg["requests"]):
        sys.exit(f"У {cfg['source']} {len(md_blocks)} блоків, у requests.json — {len(cfg['requests'])}")
    raw, lines = ep / "audio" / "raw", ep / "audio" / "lines"
    raw.mkdir(parents=True, exist_ok=True); lines.mkdir(parents=True, exist_ok=True)
    only = set(filter(None, a.only.split(",")))
    for req, block in zip(cfg["requests"], md_blocks):
        if only and req["name"] not in only:
            continue
        paras = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
        if len(paras) != len(req["ids"]):
            sys.exit(f"{req['name']}: абзаців {len(paras)}, ID {len(req['ids'])}")
        js = raw / f"{req['name']}.json"
        if js.exists():
            align = json.loads(js.read_text(encoding="utf-8"))
        elif a.cut_only:
            print(f"{req['name']}: ще не згенеровано — пропуск"); continue
        else:
            print(f"{req['name']}: генерую ({len(block)} знаків, голос {voices[req['voice']]['name']})")
            align = generate(req, paras, voices[req["voice"]]["voice_id"], cfg | {"stability": req.get("stability", cfg["stability"]), "language": req.get("language", cfg.get("language", "uk"))}, raw)
            js.write_text(json.dumps(align, ensure_ascii=False), encoding="utf-8")
        mp3 = raw / f"{req['name']}.mp3"
        sp = spans(paras, align)
        for pid, (t0, t1), s in zip(req["ids"], cut_points(sp, duration_of(mp3), silences(mp3)), sp):
            subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-i", str(mp3), "-ss", f"{t0:.3f}", "-to", f"{t1:.3f}",
                            "-c:a", "libmp3lame", "-b:a", "192k", str(lines / f"{pid}.mp3")], check=True)
            print(f"  {pid}  {t0:7.2f}–{t1:7.2f} с" + ("" if s else "  (без слів)"))


if __name__ == "__main__":
    main()

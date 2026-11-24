"""Загальна розкадровка епізоду з даних (узагальнення scripts/eNN_storyboard_build.py; механіка — як e17).

    py -3.12 -X utf8 scripts/storyboard_build.py <episode> [--out <тека>]

Вхід: episodes/<ep>/storyboard-plan.json —
  {"title", "aspect": "9:16"|"16:9", "tail": 2.0, "shift": {"NR-03": 0.5}, "scenes": {"S01": "Хук", …},
   "speakers": {"NR": "Диктор"}, "notes": "…",
   "boards": [{"id", "scene", "angle", "size", "camera", "location", "end_voice", "subject", "beats", "card"?, "phone"?, "callback"?}]}
  end_voice — кінець плану в секундах ГОЛОСУ (audio/voice-lines.json, audio-first); shift — паузи-дії, що додаються перед реплікою.
Вихід: storyboard.json (voice + boards зі start/dur у секундах фільму) і storyboard.md. Далі — tests/check_storyboard.py.
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
STUDIO = Path(__file__).resolve().parents[1]
REQ = ("id", "scene", "angle", "size", "camera", "location", "end_voice", "subject", "beats")


def build(ep: Path, plan: dict) -> dict:
    lines = json.loads((ep / "audio" / "voice-lines.json").read_text(encoding="utf-8"))
    shift, tail = plan.get("shift", {}), float(plan.get("tail", 0.0))
    acc, starts = 0.0, []
    for ln in lines:
        acc += shift.get(ln["id"], 0.0)
        starts.append((ln["start"], acc))

    def film(tv):
        s = 0.0
        for st, a in starts:
            if tv >= st - 0.25:  # кінець плану трохи перед реплікою — уже з її паузою
                s = a
        return tv + s

    voice = [{"id": ln["id"], "at": round(ln["start"] + a, 3), "dur": round(ln["end"] - ln["start"], 3),
              "text": ln["text"], "speaker": ln["id"].split("-")[0]} for ln, (_, a) in zip(lines, starts)]
    boards, t0 = [], 0.0
    pb = plan["boards"]
    for k, b in enumerate(pb):
        miss = [f for f in REQ if f not in b]
        if miss:
            raise SystemExit(f"план {b.get('id', k)}: бракує полів {miss}")
        e = film(float(b["end_voice"])) + (tail if k == len(pb) - 1 else 0.0)
        if e <= t0 + 0.3:
            raise SystemExit(f"план {b['id']}: кінець {e:.2f} с не пізніше початку {t0:.2f} с (перевір end_voice)")
        nb = {f: b[f] for f in REQ if f != "end_voice"}
        nb.update({"start": round(t0, 2), "dur": round(e - t0, 2)})
        for opt in ("card", "phone", "callback", "tech"):
            if opt in b:
                nb[opt] = b[opt]
        boards.append(nb)
        t0 = round(e, 2)
    for a, b in zip(voice, voice[1:]):
        if a["at"] + a["dur"] > b["at"] + 0.01:
            raise SystemExit(f"репліки накладаються: {a['id']}")
    if voice and voice[-1]["at"] + voice[-1]["dur"] > t0:
        raise SystemExit("голос довший за фільм — останній план має закінчуватися після останньої репліки")
    return {"_comment": f"Розкадровка ({plan.get('aspect', '9:16')}). Згенеровано scripts/storyboard_build.py зі storyboard-plan.json; "
                        "start/dur — с фільму.", "voice": voice, "boards": boards}


def to_md(plan: dict, sb: dict, ep_name: str) -> str:
    who = plan.get("speakers", {})
    md = [f"# Розкадровка — {plan.get('title', ep_name)} ({plan.get('aspect', '9:16')})", "",
          f"Згенеровано `scripts/storyboard_build.py {ep_name}` зі `storyboard-plan.json`. {plan.get('notes', '')}", ""]
    for sc, name in plan.get("scenes", {}).items():
        md += [f"## {sc} · {name}", "", "| План | Час | Ракурс / крупність | Що в кадрі | Біти руху | Голос |", "|---|---|---|---|---|---|"]
        for b in sb["boards"]:
            if b["scene"] != sc:
                continue
            a, e = b["start"], b["start"] + b["dur"]
            vo = " ".join(f"{who.get(v['speaker'], v['speaker'])}: «{v['text']}»" for v in sb["voice"] if a - 0.05 <= v["at"] < e - 0.05)
            md.append(f"| {b['id']} | {a:.1f}–{e:.1f} ({b['dur']:.1f} с) | {b['angle']} {b['size']} | {b['subject']} | {b['beats']} | {vo} |")
        md.append("")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--out")
    a = ap.parse_args()
    ep = STUDIO / "episodes" / a.episode
    out = Path(a.out) if a.out else ep
    plan = json.loads((ep / "storyboard-plan.json").read_text(encoding="utf-8"))
    sb = build(ep, plan)
    out.mkdir(parents=True, exist_ok=True)
    (out / "storyboard.json").write_text(json.dumps(sb, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "storyboard.md").write_text(to_md(plan, sb, a.episode), encoding="utf-8")
    ds = sorted(b["dur"] for b in sb["boards"])
    print(f"✓ storyboard: {len(ds)} планів, {sum(ds):.1f} с, "
          f"медіана {ds[len(ds) // 2]:.2f}, мін {ds[0]}, макс {ds[-1]}")


if __name__ == "__main__":
    main()

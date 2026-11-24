"""Експорт субтитрів епізоду у SRT для YouTube.

    py -3.12 scripts/export_srt.py episode-001

Довгі репліки переносяться на ≤2 рядки по ~42 символи (злам по словах, збалансовано),
щоб YouTube не обрізав і не ламав рядок посеред слова.
"""

import json
import sys
from pathlib import Path

STUDIO = Path(__file__).resolve().parents[1]
MAX = 42  # рекомендація YouTube: ~42 символи в рядку, максимум 2 рядки


def ts(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def wrap(text):
    """Злам на ≤2 рядки по словах, якомога рівніше; якщо не влазить — найближчий до середини пробіл."""
    text = " ".join(text.split())  # прибрати подвійні пробіли/переноси
    if len(text) <= MAX:
        return text
    words = text.split(" ")
    # шукаємо точку зламу, що мінімізує різницю довжин двох рядків і тримає обидва ≤ MAX (якщо можливо)
    best = None
    for i in range(1, len(words)):
        a = " ".join(words[:i])
        b = " ".join(words[i:])
        over = max(0, len(a) - MAX) + max(0, len(b) - MAX)
        diff = abs(len(a) - len(b))
        score = (over, diff)
        if best is None or score < best[0]:
            best = (score, a, b)
    return best[1] + "\n" + best[2]


def main():
    ep = STUDIO / "episodes" / sys.argv[1]
    cues = json.loads((ep / "subtitles" / "subtitles.json").read_text(encoding="utf-8"))["cues"]
    blocks = []
    for i, c in enumerate(cues, 1):
        txt = wrap(c["text"].replace("*", ""))
        blocks.append(f"{i}\n{ts(c['start'])} --> {ts(c['end'])}\n{txt}\n")
    srt = "\n".join(blocks)
    out = ep / "subtitles" / f"{sys.argv[1]}.uk.srt"
    out.write_text(srt, encoding="utf-8", newline="\r\n")  # CRLF — безпечніше для плеєрів/YouTube
    over = sum(1 for c in cues for ln in wrap(c["text"].replace("*", "")).split("\n") if len(ln) > MAX)
    print("SRT →", out)
    print(f"  реплік: {len(cues)}; рядків усе ще >{MAX} символів: {over}")


if __name__ == "__main__":
    main()

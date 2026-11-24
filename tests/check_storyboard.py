"""Перевірка розкадровки на повтори (channel/storyboard-rules.md). Запуск: py -3.12 tests/check_storyboard.py episodes/<ep>/storyboard.json

storyboard.json: {"boards": [{"id", "scene", "angle", "size", "camera", "location", "subject", "phone"?}]}
  angle: top | front | back | side | low | high | ots | pov | insert | eye
  size:  ECU | CU | MCU | MS | WS | EWS
  phone: "world" (у руці персонажа — розмір з props.tsx) | "insert" (HandPhone) | відсутнє

Помилки (exit 1):
  R1  сусідні кадри з однаковими angle І size (майже однакові кадри);
  R2  два «insert» поспіль (телефон на весь кадр двічі підряд);
  R3  сусідні кадри з однаковою камерою (поле camera однакове);
  R4  той самий angle три кадри поспіль;
  R5  телефон у кадрі з персонажем не позначений як "world" (тобто не в метрах).
Попередження: однакові angle сусідів при різній крупності (дозволено, якщо крупність різниться ≥ 2 кроки).
"""
import json, sys
sys.stdout.reconfigure(encoding="utf-8")

SIZES = ["ECU", "CU", "MCU", "MS", "WS", "EWS"]


def main(path):
    boards = json.load(open(path, encoding="utf-8"))["boards"]
    err, warn = [], []
    for i, b in enumerate(boards):
        if b.get("size") not in SIZES:
            err.append(f"{b['id']}: невідома крупність {b.get('size')}")
        if b.get("phone") == "prop-unscaled":
            err.append(f"R5 {b['id']}: телефон не в метрах")
        if i == 0:
            continue
        a = boards[i - 1]
        if a.get("card") or b.get("card"):
            continue
        if b.get("callback") == a["id"]:  # свідомий повтор кадру (storyboard-rules §1) — позначений полем callback
            warn.append(f"{a['id']}→{b['id']}: callback (той самий кадр навмисно)")
            continue
        if a["angle"] == b["angle"] and a["size"] == b["size"]:
            err.append(f"R1 {a['id']}→{b['id']}: однакові ракурс ({b['angle']}) і крупність ({b['size']})")
        elif a["angle"] == b["angle"] and abs(SIZES.index(a["size"]) - SIZES.index(b["size"])) < 2:
            err.append(f"R1 {a['id']}→{b['id']}: той самий ракурс ({b['angle']}), крупність різниться менш ніж на 2 кроки")
        if a["angle"] == "insert" and b["angle"] == "insert":
            err.append(f"R2 {a['id']}→{b['id']}: дві вставки телефона поспіль")
        if a.get("camera") and a.get("camera") == b.get("camera"):
            err.append(f"R3 {a['id']}→{b['id']}: та сама камера")
        if i >= 2 and boards[i - 2]["angle"] == a["angle"] == b["angle"]:
            err.append(f"R4 {boards[i-2]['id']}…{b['id']}: ракурс {b['angle']} тричі поспіль")
    for e in err:
        print("ПОМИЛКА", e)
    for w in warn:
        print("увага", w)
    print(f"check_storyboard: {len(boards)} кадрів, помилок {len(err)}")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "episodes/episode-003/storyboard.json"))

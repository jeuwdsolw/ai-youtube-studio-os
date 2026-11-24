"""Підставний `claude` для тестів: відповіді з черги файлів FAKE_CLAUDE_DIR/resp-NNN.json.

resp: {"structured_output": {...}, "result": "...", "is_error": false, "returncode": 0,
       "write": {"відносний/шлях": "вміст"}}  — write імітує зміни файлів агентною сесією (відносно cwd).
Кожен виклик зберігає prompt-NNN.txt і args-NNN.json для перевірок.
"""
import json
import os
import sys
from pathlib import Path

d = Path(os.environ["FAKE_CLAUDE_DIR"])
if "--version" in sys.argv:
    print("0.0.0 (fake)")
    sys.exit(0)
prompt = sys.stdin.buffer.read().decode("utf-8")   # справжній claude читає UTF-8
queue = sorted(d.glob("resp-*.json"))
n = len(list(d.glob("prompt-*.txt"))) + 1
(d / f"prompt-{n:03d}.txt").write_text(prompt, encoding="utf-8")
(d / f"args-{n:03d}.json").write_text(json.dumps(sys.argv[1:], ensure_ascii=False), encoding="utf-8")
if not queue:
    print(json.dumps({"is_error": True, "result": "fake: немає відповіді"}))
    sys.exit(1)
resp = json.loads(queue[0].read_text(encoding="utf-8"))
queue[0].rename(queue[0].with_suffix(".used"))
for rel, content in resp.get("write", {}).items():
    p = Path.cwd() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
out = {"type": "result", "subtype": "success", "is_error": resp.get("is_error", False),
       "result": resp.get("result", json.dumps(resp.get("structured_output", {}), ensure_ascii=False)),
       "structured_output": resp.get("structured_output"), "total_cost_usd": 0.01, "num_turns": 3,
       "session_id": f"fake-{n}"}
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps(out, ensure_ascii=False))
sys.exit(resp.get("returncode", 0))

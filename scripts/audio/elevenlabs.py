"""ElevenLabs API без залежностей. Ключ — ELEVENLABS_API_KEY (змінна середовища або .env у корені студії).

py -3.12 -X utf8 scripts/audio/elevenlabs.py account
py -3.12 -X utf8 scripts/audio/elevenlabs.py search [gender] [age] [пошук]   # бібліотека, мова uk, за популярністю
py -3.12 -X utf8 scripts/audio/elevenlabs.py my                               # голоси в моїй бібліотеці
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "https://api.elevenlabs.io"


def key() -> str:
    k = os.environ.get("ELEVENLABS_API_KEY")
    env = ROOT / ".env"
    if not k and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ELEVENLABS_API_KEY="):
                k = line.split("=", 1)[1].strip()
    if not k:
        sys.exit("Немає ELEVENLABS_API_KEY (.env у корені студії)")
    return k


def api(path, body=None, params=None, raw=False, method=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"xi-api-key": key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            out = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {path}: {e.read().decode(errors='replace')[:500]}") from None
    return out if raw else json.loads(out)


def search(gender=None, age=None, query=None, page_size=30):
    params = {"language": "uk", "sort": "usage_character_count_1y", "page_size": page_size}
    params |= {k: v for k, v in {"gender": gender, "age": age, "search": query}.items() if v}
    return api("/v1/shared-voices", params=params)["voices"]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "account"
    if cmd == "account":
        s = api("/v1/user/subscription")
        print(s["tier"], f'{s["character_count"]}/{s["character_limit"]} знаків', "voice slots",
              s.get("voice_slots_used"), "/", s.get("voice_limit"))
    elif cmd == "search":
        a = [x if x != "-" else None for x in sys.argv[2:]] + [None] * 3
        for v in [v for v in search(*a[:3]) if v.get("language") == "uk"]:
            print(f'{v["usage_character_count_1y"] or 0:>12,}  {v["cloned_by_count"] or 0:>6}  {v["voice_id"]}  '
                  f'{v["gender"]:<6} {v["age"]:<11} {v.get("accent") or "":<10} {v["name"][:40]:<40} {(v.get("descriptive") or "")[:14]}  '
                  f'{(v.get("description") or "").replace(chr(10), " ")[:90]}')
    elif cmd == "my":
        for v in api("/v2/voices", params={"page_size": 100})["voices"]:
            print(v["voice_id"], v["category"], v["name"])

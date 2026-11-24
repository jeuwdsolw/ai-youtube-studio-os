"""Спільні помічники агентів: каталоги канону, епізоди, лінтер тексту."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..config import Config

REAL_BRANDS = ["glovo", "bolt", "uber", "facetime", "viber", "telegram", "whatsapp", "iphone", "samsung",
               "instagram", "tiktok", "monobank", "privatbank", "приватбанк", "нова пошта", "rozetka", "сільпо", "атб"]


def read(root: Path, rel: str, limit: int | None = None) -> str:
    p = root / rel
    if not p.exists():
        return ""
    t = p.read_text(encoding="utf-8")
    return t[:limit] if limit else t


def characters(root: Path) -> list[dict]:
    """Канон персонажів: id, ім'я, роль, статус (канон/чернетка — з _comment)."""
    out = []
    for f in sorted((root / "channel" / "characters").glob("*/*.json")):
        if f.name == "expressions.json" or f.stem != f.parent.name:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        c = str(d.get("_comment", "")).lower()
        out.append({"id": d.get("id", f.stem), "name": d.get("name", ""), "role": d.get("role", "")[:140],
                    "status": "draft" if "чернетк" in c else "canon"})
    return out


def locations(root: Path) -> list[dict]:
    out = []
    for f in sorted((root / "channel" / "locations").glob("*.md")):
        first = f.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
        out.append({"id": f.stem, "title": first[:120]})
    return out


def narrator(cfg: Config) -> tuple[str, dict]:
    p = cfg.production
    return p.get("narrator_voice", "NR"), {"voice_id": p.get("narrator_voice_id", "tZssYepgGaQmegsMEXjK"),
                                           "name": p.get("narrator_voice_name", "True - Crime & Horror Narrator")}


def voices(root: Path, cfg: Config | None = None) -> dict[str, dict]:
    """Голоси ElevenLabs, уже використані студією (ключ → voice_id, ім'я). Нових голосів агент не вигадує.
    Диктор закріплений у config (True Crime) і перекриває старі значення ключа NR з ранніх епізодів."""
    out: dict[str, dict] = {}
    if cfg is not None:
        k, v = narrator(cfg)
        out[k] = v
        if cfg.production.get("narrator_only", True):
            return out
    for f in sorted((root / "episodes").glob("*/voice/voices.json")):
        try:
            for k, v in json.loads(f.read_text(encoding="utf-8")).items():
                if isinstance(v, dict) and v.get("voice_id"):
                    out.setdefault(k, {"voice_id": v["voice_id"], "name": v.get("name", "")})
        except (json.JSONDecodeError, OSError):
            continue
    return out


def episode_titles(root: Path) -> list[dict]:
    out = []
    for f in sorted((root / "episodes").glob("*/script.md")):
        head = next((ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.startswith("#")), "")
        out.append({"dir": f.parent.name, "title": head.lstrip("# ").strip()[:120]})
    return out


def recent_scripts(root: Path, n: int = 10) -> list[str]:
    fs = sorted((root / "episodes").glob("*/script.md"), key=lambda p: p.stat().st_mtime)[-n:]
    return [f.read_text(encoding="utf-8") for f in fs]


def next_short_dir(root: Path) -> str:
    nums = [int(m.group(1)) for p in (root / "episodes").glob("episode-*")
            if (m := re.fullmatch(r"episode-(\d+)", p.name))]
    return f"episode-{(max(nums) + 1 if nums else 1):03d}"


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "untitled"


def episode_dir(con: sqlite3.Connection, cfg: Config, video_id: str) -> Path | None:
    row = con.execute("SELECT episode_dir FROM videos WHERE id=?", (video_id,)).fetchone()
    return cfg.root / row["episode_dir"] if row and row["episode_dir"] else None


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", t.lower())).strip()


def lint_text(text: str, cfg: Config, previous: list[str]) -> dict:
    """Перевірка тексту сценарію/озвучки: штампи (і їх повтор з попередніх відео), бренди, повтори, довгі речення."""
    t = norm(text)
    errors, warnings = [], []
    prev = " ".join(norm(p) for p in previous)
    for ph in cfg.production.get("stock_phrases", []):
        p = norm(ph)
        if p in t:
            (errors if p in prev else warnings).append(
                f"штамп «{ph}»" + (" — уже був у попередніх відео" if p in prev else ""))
    for b in REAL_BRANDS:
        if re.search(rf"(?<!\w){re.escape(b)}(?!\w)", t):
            errors.append(f"реальний бренд «{b}» — замінити вигаданим")
    sents = [s.strip() for s in re.split(r"[.!?…]+", text) if s.strip()]
    long_s = [s for s in sents if len(s.split()) > 22]
    if long_s:
        warnings.append(f"{len(long_s)} задовгих речень (>22 слів), напр.: «{long_s[0][:80]}…»")
    words = t.split()
    grams = {}
    for i in range(len(words) - 4):
        g = " ".join(words[i:i + 5])
        grams[g] = grams.get(g, 0) + 1
    rep = [g for g, c in grams.items() if c > 1]
    if rep:
        warnings.append(f"повтор фраз усередині тексту: «{rep[0]}»")
    return {"errors": errors, "warnings": warnings}


def count_words(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", re.sub(r"\[[^\]]*\]", "", text)))

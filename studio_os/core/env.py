"""Читання ключів з .env студії (без сторонніх бібліотек). Значення ніколи не логуються."""
from __future__ import annotations

import os
from pathlib import Path

from .. import STUDIO


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or STUDIO / ".env"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get(key: str, path: Path | None = None) -> str | None:
    return os.environ.get(key) or load_env(path).get(key) or None

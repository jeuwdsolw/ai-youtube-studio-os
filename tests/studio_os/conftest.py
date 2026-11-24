from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

STUDIO = Path(__file__).resolve().parents[2]
if str(STUDIO) not in sys.path:
    sys.path.insert(0, str(STUDIO))

from studio_os import config as config_mod  # noqa: E402
from studio_os.agents import base  # noqa: E402
from studio_os.core import clock, db  # noqa: E402


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def fclock():
    c = FakeClock(datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc))  # пн, 11:00 Київ
    clock.set_now(c)
    yield c
    clock.set_now(None)


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "studio"
    (root / "config").mkdir(parents=True)
    shutil.copy(STUDIO / "config" / "studio.toml", root / "config" / "studio.toml")
    c = config_mod.load(root / "config" / "studio.toml", root=root)
    for k in ("max_in_progress", "auto_formats"):   # ліміти конвеєра тестуються окремо (test_conveyor_*)
        c.production.pop(k, None)
    return c


@pytest.fixture
def con(cfg):
    c = db.open_db(cfg.db_path)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_real_claude(tmp_path, monkeypatch):
    """Жоден тест не викликає справжній Claude Code (ліміт підписки!): за замовчуванням — підставний бінарник
    з порожньою чергою відповідей (повертає помилку). Тести агентів підміняють чергу фікстурою `fake`."""
    d = tmp_path / "no_claude"
    d.mkdir()
    monkeypatch.setenv("STUDIO_CLAUDE_BIN", str(STUDIO / "tests" / "studio_os" / "fake_claude.py"))
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(d))
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    # вільне місце справжнього диска не має впливати на тести (тести диска підміняють це самі)
    from studio_os.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "_free_gb", lambda self: 1000.0)
    from studio_os.adapters import youtube_upload        # жодних справжніх запитів до Google/YouTube у тестах

    def _no_net(*a, **k):
        raise AssertionError("тест звернувся до справжнього YouTube")
    monkeypatch.setattr(youtube_upload, "_http", _no_net)


@pytest.fixture
def registry():
    """Тимчасові агенти для тестів: справжні агенти етапів прибрано, реєстр відновлюється після тесту."""
    saved = dict(base.REGISTRY)
    for k in [k for k, v in base.REGISTRY.items() if v.stage is not None]:
        del base.REGISTRY[k]
    yield base.REGISTRY
    base.REGISTRY.clear()
    base.REGISTRY.update(saved)

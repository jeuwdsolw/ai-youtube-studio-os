"""Базовий контракт агента.

Агент отримує `Context` + payload задачі й повертає `AgentResult`. Він ніколи не «мовчки пропускає» роботу:
- успіх → AgentResult(ok=True, ...);
- тимчасова помилка (мережа, зайнятий ресурс) → raise RetryableError → черга повторить із затримкою;
- постійна помилка / потрібна людина → raise BlockedError → етап `needs_human`, подія в журналі.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, ClassVar

from ..config import Config
from ..errors import BlockedError, RetryableError  # noqa: F401 — реекспорт для агентів


@dataclass
class Context:
    con: sqlite3.Connection
    cfg: Config
    job_id: int
    video_id: str | None
    stage: str | None
    heartbeat: Callable[[], bool]          # продовжити оренду під час довгої роботи
    log: Callable[[str, str], None]        # log(level, message) → журнал із прив'язкою до задачі
    should_stop: Callable[[], bool] = lambda: False   # STOP/пауза: довгі агенти перевіряють між кроками


@dataclass
class AgentResult:
    ok: bool = True
    summary: str = ""
    data: dict = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)   # шляхи відносно студії


class Agent:
    name: ClassVar[str]                     # = kind задачі
    stage: ClassVar[str | None] = None      # етап конвеєра, який агент виконує (None — службова задача)
    resources: ClassVar[tuple[str, ...]] = ()
    max_attempts: ClassVar[int] = 3

    def run(self, ctx: Context, payload: dict) -> AgentResult:  # pragma: no cover - абстрактний
        raise NotImplementedError


REGISTRY: dict[str, type[Agent]] = {}


def register(cls: type[Agent]) -> type[Agent]:
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ValueError(f"агент {cls.name!r} уже зареєстрований")
    REGISTRY[cls.name] = cls
    return cls


def for_stage(stage: str) -> type[Agent] | None:
    for cls in REGISTRY.values():
        if cls.stage == stage:
            return cls
    return None

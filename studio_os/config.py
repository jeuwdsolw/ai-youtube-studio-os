"""Завантаження й перевірка config/studio.toml (лише стандартна бібліотека)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import STUDIO

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FORMATS = ("short", "long")
# Порядок етапів конвеєра (audio-first: голос до розкадровки — channel/production-optimization.md §C).
STAGES = [
    "research", "planning", "script", "voice", "storyboard", "animation",
    "edit", "thumbnail", "qc", "approval", "publish",
]
MODES = ("manual", "semi", "auto", "conveyor")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Slot:
    format: str
    weekday: int  # 0 = пн
    at: time


@dataclass(frozen=True)
class Mode:
    name: str
    auto_start: bool
    gates: frozenset[str]
    auto_publish: bool


@dataclass(frozen=True)
class Budget:
    name: str
    period: str
    limit: int


@dataclass
class Config:
    root: Path
    tz: ZoneInfo
    mode: str
    plan_horizon_weeks: int
    db_path: Path
    log_dir: Path
    backup_dir: Path
    backups_keep: int
    lease_seconds: int
    min_free_disk_gb: float
    slots: list[Slot]
    deadlines: dict[str, dict[str, int]]
    modes: dict[str, Mode]
    exclusive: frozenset[str]
    budgets: dict[str, Budget]
    maintenance: dict = field(default_factory=dict)
    claude: dict = field(default_factory=dict)
    research: dict = field(default_factory=dict)
    production: dict = field(default_factory=dict)
    qc: dict = field(default_factory=dict)
    youtube: dict = field(default_factory=dict)

    def mode_rules(self, name: str | None = None) -> Mode:
        return self.modes[name or self.mode]


def _parse_time(s: str) -> time:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"Неправильний час '{s}' (очікується HH:MM)") from e


def load(path: Path | None = None, root: Path | None = None) -> Config:
    root = root or STUDIO
    path = path or Path(os.environ.get("STUDIO_CONFIG", root / "config" / "studio.toml"))
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    st = raw.get("studio", {})

    slots: list[Slot] = []
    for s in raw.get("schedule", []):
        fmt = s.get("format")
        if fmt not in FORMATS:
            raise ConfigError(f"schedule.format має бути одним з {FORMATS}, а не {fmt!r}")
        for d in s.get("days", []):
            if d not in DAYS:
                raise ConfigError(f"Невідомий день {d!r}")
            slots.append(Slot(fmt, DAYS.index(d), _parse_time(s["time"])))
    if not slots:
        raise ConfigError("Розклад порожній")

    deadlines = {}
    for fmt in FORMATS:
        d = raw.get("deadlines", {}).get(fmt, {})
        unknown = set(d) - set(STAGES)
        if unknown:
            raise ConfigError(f"deadlines.{fmt}: невідомі етапи {sorted(unknown)}")
        deadlines[fmt] = {k: int(v) for k, v in d.items()}

    modes = {}
    for name in MODES:
        m = raw.get("modes", {}).get(name)
        if m is None:
            raise ConfigError(f"Немає [modes.{name}]")
        gates = frozenset(m.get("gates", []))
        if gates - set(STAGES):
            raise ConfigError(f"modes.{name}.gates: невідомі етапи {sorted(gates - set(STAGES))}")
        modes[name] = Mode(name, bool(m.get("auto_start")), gates, bool(m.get("auto_publish")))

    mode = st.get("mode", "manual")
    if mode not in MODES:
        raise ConfigError(f"studio.mode має бути одним з {MODES}")

    budgets = {
        k: Budget(k, v.get("period", "day"), int(v.get("limit", 0)))
        for k, v in raw.get("budgets", {}).items()
    }
    for b in budgets.values():
        if b.period not in ("day", "week", "month"):
            raise ConfigError(f"budgets.{b.name}.period: {b.period!r}")

    def p(key: str, default: str) -> Path:
        v = Path(st.get(key, default))
        return v if v.is_absolute() else root / v

    return Config(
        root=root,
        tz=ZoneInfo(st.get("timezone", "Europe/Kyiv")),
        mode=mode,
        plan_horizon_weeks=int(st.get("plan_horizon_weeks", 3)),
        db_path=p("db_path", "data/studio.db"),
        log_dir=p("log_dir", "data/logs"),
        backup_dir=p("backup_dir", "data/backups"),
        backups_keep=int(st.get("backups_keep", 14)),
        lease_seconds=int(st.get("lease_seconds", 900)),
        min_free_disk_gb=float(st.get("min_free_disk_gb", 10)),
        slots=slots,
        deadlines=deadlines,
        modes=modes,
        exclusive=frozenset(raw.get("resources", {}).get("exclusive", [])),
        budgets=budgets,
        maintenance=raw.get("maintenance", {}),
        claude=raw.get("claude", {}),
        research=raw.get("research", {}),
        production=raw.get("production", {}),
        qc=raw.get("qc", {}),
        youtube=raw.get("youtube", {}),
    )

"""Quality Control (Етап 4): 8 модулів, ваги, критичні прапорці, задачі на виправлення."""
from __future__ import annotations

from pathlib import Path

from . import modules


def run(root: Path, ep: Path, fmt: str, cfg, *, llm: bool = True) -> dict:
    auto = modules.run_auto(root, ep, fmt, cfg)
    rv = None
    if llm:
        from . import review
        rv = review.review(root, ep, fmt, cfg, auto)
    rep = modules.aggregate(auto, rv, (getattr(cfg, "qc", {}) or {}).get("weights"))
    if rv:
        rep["review_images"] = rv.get("images", [])
    return rep

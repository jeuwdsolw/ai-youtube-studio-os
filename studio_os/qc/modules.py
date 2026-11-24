"""8 модулів QC (docs/studio-os/01-audit-and-architecture.md §4): детерміновані перевірки + об'єднання з рецензією Claude.

Проблема (issue): {module, severity: critical|major|minor, msg, stage, scope, criterion, source: auto|llm}.
`stage` — етап, що має виправити проблему (цикл виправлень), `criterion` — як перевірити, що виправлено.
Бал модуля: 100 − штрафи (critical 60, major 25, minor 8), не нижче 0; якщо є оцінка рецензента — середнє обох.
Загальний бал — зважене середнє за вагами `[qc.weights]`; модуль без даних не враховується (вага перерозподіляється).
Будь-яка critical → BLOCKED_CRITICAL, незалежно від бала.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ..agents import common
from . import media

MODULES = {
    "story": "A Story Logic", "dialogue": "B Dialogue", "retention": "C Retention", "animation": "D Animation",
    "style": "E Visual Style", "audio": "F Audio", "editing": "G Editing", "youtube": "H YouTube Readiness",
}
DEFAULT_WEIGHTS = {"story": 20, "dialogue": 10, "retention": 15, "animation": 15, "style": 15, "audio": 10,
                   "editing": 10, "youtube": 5}
PENALTY = {"critical": 60, "major": 25, "minor": 8}


def issue(module, severity, msg, stage, criterion, scope="епізод", source="auto") -> dict:
    return {"module": module, "severity": severity, "msg": msg, "stage": stage, "scope": scope,
            "criterion": criterion, "source": source}


def verdict_for(score: float) -> str:
    if score >= 90:
        return "APPROVED"
    if score >= 80:
        return "APPROVED_WITH_WARNINGS"
    if score >= 60:
        return "NEEDS_REVISION"
    return "REJECTED"


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def youtube_meta(ep: Path) -> dict:
    """Назви/опис/теги з youtube.md (формат студії, напр. episode-017/youtube.md)."""
    p = ep / "youtube.md"
    if not p.exists():
        return {}
    t = p.read_text(encoding="utf-8")
    sec = {}
    cur = None
    for ln in t.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip().lower()
            sec[cur] = []
        elif cur:
            sec[cur].append(ln)
    titles_sec = next((v for k, v in sec.items() if "назв" in k), [])
    titles = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip() for ln in titles_sec if re.match(r"^\s*\d+[.)]\s", ln)]
    desc = "\n".join(next((v for k, v in sec.items() if k.startswith("опис")), [])).strip()
    tags = " ".join(next((v for k, v in sec.items() if k.startswith("теги")), [])).strip()
    return {"titles": titles, "description": desc, "tags": tags,
            "materials": any("матеріал" in k for k in sec), "text": t}


def other_titles(root: Path, ep: Path) -> set[str]:
    out = set()
    for f in (root / "episodes").glob("*/youtube.md"):
        if f.parent == ep:
            continue
        for t in youtube_meta(f.parent).get("titles", []):
            out.add(common.norm(t))
    return out


def run_auto(root: Path, ep: Path, fmt: str, cfg) -> dict:
    """Детерміновані перевірки. Повертає {issues, measures, not_checked, frames_dir}."""
    q = getattr(cfg, "qc", {}) or {}
    issues: list[dict] = []
    measures: dict = {}
    not_checked: list[str] = []
    want = (1080, 1920) if fmt == "short" else (1920, 1080)
    video = ep / "renders" / "final_master.mp4"
    if not video.exists():   # Long-епізоди власника: <назва>_final_master.mp4
        video = next(iter(sorted((ep / "renders").glob("*final_master.mp4"))), video) if (ep / "renders").exists() else video
    measures["file"] = video.relative_to(root).as_posix() if video.is_relative_to(root) else str(video)
    if not video.exists():
        issues.append(issue("editing", "critical", "немає renders/final_master.mp4", "edit", "файл існує й проходить check_video"))
        return {"issues": issues, "measures": measures, "not_checked": ["відео відсутнє — модулі D/F/G не перевірено"],
                "frames_dir": None}

    # ---- G Editing ----
    pr = media.probe(video)
    measures["video"] = {"size": [pr.width, pr.height], "fps": pr.fps, "duration_s": round(pr.duration, 2), "audio": pr.audio}
    if (pr.width, pr.height) != want:
        issues.append(issue("editing", "critical", f"роздільність {pr.width}×{pr.height}, для {fmt} потрібно {want[0]}×{want[1]}",
                            "edit", f"final_master.mp4 {want[0]}×{want[1]}"))
    if pr.fps not in ("30/1", "30000/1000"):
        issues.append(issue("editing", "major", f"fps {pr.fps}, стандарт студії 30", "edit", "30 fps"))
    lo, hi = (q.get("short_target_s") or cfg.production.get("short_target_s", [45, 75])) if fmt == "short" else (None, None)
    if fmt == "short":
        if pr.duration > float(q.get("short_max_s", 180)):
            issues.append(issue("editing", "critical", f"тривалість {pr.duration:.1f} с > ліміту Shorts {q.get('short_max_s', 180)} с",
                                "edit", "тривалість ≤ ліміту Shorts"))
        elif not (lo - 5 <= pr.duration <= hi + 10):
            issues.append(issue("retention", "minor", f"тривалість {pr.duration:.1f} с поза ціллю {lo}–{hi} с", "script",
                                f"тривалість {lo}–{hi} с"))
    blk = media.inner(media.black_segments(video, float(q.get("black_min_s", 0.5))), pr.duration,
                      float(q.get("head_s", 0.6)), float(q.get("tail_s", 2.0)))
    measures["black_segments"] = blk
    for a, b in blk:
        issues.append(issue("editing", "critical", f"чорний екран {a:.2f}–{b:.2f} с", "edit",
                            "blackdetect без відрізків усередині відео (або навмисне — описати в script.md і --still)",
                            scope=f"{a:.1f}–{b:.1f} с"))
    dark = [s for s in media.inner(media.black_segments(video, float(q.get("dark_min_s", 0.6)), 0.10), pr.duration,
                                   float(q.get("head_s", 0.6)), float(q.get("tail_s", 2.0)))
            if not any(a <= s[0] < b for a, b in blk)]
    measures["dark_segments"] = dark
    for a, b in dark:
        issues.append(issue("style", "minor", f"майже чорний план {a:.1f}–{b:.1f} с (деталі ледь видно на телефоні)", "animation",
                            "у темному плані читається силует/джерело світла (STYLE-GUIDE: не всі кадри однаково темні)",
                            scope=f"{a:.1f}–{b:.1f} с"))
    srt = sorted((ep / "subtitles").glob("*.srt")) if (ep / "subtitles").exists() else []
    if not srt:
        issues.append(issue("editing", "major", "немає SRT-субтитрів (subtitles/*.srt)", "edit", "scripts/export_srt.py створив SRT"))
    else:
        cues = re.findall(r"\d+\n[\d:,]+ --> [\d:,]+\n(.+?)(?:\n\n|\Z)", srt[0].read_text(encoding="utf-8").replace("\r", ""), re.S)
        measures["srt_cues"] = len(cues)
        longest = max((len(ln) for c in cues for ln in c.splitlines()), default=0)
        if longest > int(q.get("subtitle_max_chars", 42)):
            issues.append(issue("editing", "minor", f"рядок субтитрів {longest} знаків (> {q.get('subtitle_max_chars', 42)})", "edit",
                                "рядки субтитрів ≤ 42 знаків"))

    # ---- D Animation + F Audio (наявний check_video.py) ----
    cv = media.check_video(root, video)
    measures["check_video"] = {"pass": cv.passed, "static_secs": cv.static_secs, "lufs": cv.lufs, "true_peak": cv.true_peak,
                               "max_volume": cv.max_volume, "failed": [m for ok, m in cv.lines if not ok]}
    n_sec = max(1, int(pr.duration))
    if len(cv.static_secs) > max(2, 0.1 * n_sec):
        issues.append(issue("animation", "critical", f"масова статика: {len(cv.static_secs)} нерухомих секунд {cv.static_secs[:12]}",
                            "animation", "check_video: нерухомих секунд ≤ 1 (motion_check STATIC 0)"))
    elif len(cv.static_secs) > 1:
        issues.append(issue("animation", "major", f"нерухомі секунди {cv.static_secs}", "animation",
                            "check_video: нерухомих секунд ≤ 1", scope=", ".join(f"{s} с" for s in cv.static_secs)))
    frz = media.inner(media.freeze_segments(video, float(q.get("freeze_min_s", 2.0))), pr.duration,
                      float(q.get("head_s", 0.6)), float(q.get("tail_s", 2.0)))
    measures["freeze_segments"] = frz
    for a, b in frz:
        issues.append(issue("animation", "major", f"застиглий кадр {a:.1f}–{b:.1f} с (freezedetect)", "animation",
                            "у плані є рух персонажа/середовища/камери", scope=f"{a:.1f}–{b:.1f} с"))
    if pr.audio != 1:
        issues.append(issue("audio", "critical", f"аудіодоріжок: {pr.audio}", "edit", "одна аудіодоріжка"))
    if cv.max_volume is not None and cv.max_volume > -0.1:
        issues.append(issue("audio", "critical", f"кліпінг: пік {cv.max_volume} dB", "edit", "пік ≤ −0.1 dB (master.py)"))
    if cv.lufs is not None and not (-15.5 <= cv.lufs <= -12.5):
        issues.append(issue("audio", "major", f"гучність {cv.lufs} LUFS (YouTube −14 ± 1.5)", "edit", "scripts/audio/master.py → −14 LUFS"))
    if cv.true_peak is not None and cv.true_peak > -0.9:
        issues.append(issue("audio", "major", f"true peak {cv.true_peak} dBTP (> −1)", "edit", "true peak ≤ −1 dBTP"))
    if cv.lufs is None:
        not_checked.append("LUFS/true peak (ebur128 не дав результату)")
    sil = media.inner(media.silence_segments(video, float(q.get("silence_min_s", 2.5))), pr.duration,
                      float(q.get("head_s", 0.6)), float(q.get("tail_s", 2.0)))
    measures["silence_segments"] = sil
    for a, b in sil:
        issues.append(issue("audio", "minor", f"повна тиша {a:.1f}–{b:.1f} с", "edit", "під паузами — атмосфера/SFX, не цифрова тиша",
                            scope=f"{a:.1f}–{b:.1f} с"))

    # ---- C Retention (голос) + B Dialogue ----
    lines = _json(ep / "audio" / "voice-lines.json") or []
    if lines:
        first = float(lines[0].get("start", 0))
        measures["hook_s"] = first
        hook_max = float(q.get("short_hook_s", 3.0))
        if fmt == "short" and first > hook_max:
            issues.append(issue("retention", "critical", f"слабкий хук: перша репліка на {first:.1f} с (> {hook_max} с)", "edit",
                                f"перша репліка або подія ≤ {hook_max} с"))
        gaps = [(float(b.get("start", 0)) - float(a.get("end", 0)), a.get("id"), b.get("id")) for a, b in zip(lines, lines[1:])]
        gmax = float(q.get("short_gap_s", 4.0) if fmt == "short" else q.get("long_gap_s", 8.0))
        for g, a, b in gaps:
            if g > gmax:
                issues.append(issue("retention", "minor", f"пауза без голосу {g:.1f} с між {a} і {b}", "edit",
                                    f"пауза ≤ {gmax} с або заповнена дією", scope=f"{a}→{b}"))
        for ln in lines:
            words = common.count_words(ln.get("text", ""))
            dur = float(ln.get("end", 0)) - float(ln.get("start", 0))
            if words >= 6 and dur > 0 and words / dur > float(q.get("max_words_per_s", 4.2)):   # короткі репліки — недостовірно
                issues.append(issue("audio", "major", f"репліка {ln.get('id')}: {words} слів за {dur:.1f} с — можливо обрізана/пришвидшена",
                                    "voice", "темп репліки ≈ 140 сл/хв, кінець не обрізаний", scope=str(ln.get("id"))))
        prev = [t for f, t in _scripts_except(root, ep)]
        lint = common.lint_text(" ".join(ln.get("text", "") for ln in lines), cfg, prev)
        for e in lint["errors"]:
            issues.append(issue("dialogue", "major", e, "script", "лінтер тексту без помилок"))
        for w in lint["warnings"]:
            issues.append(issue("dialogue", "minor", w, "script", "лінтер тексту без попереджень"))
    else:
        issues.append(issue("retention", "major", "немає audio/voice-lines.json — хук і паузи не перевірено", "voice",
                            "voice-lines.json існує"))

    # ---- D: розкадровка (наявний check_storyboard.py) ----
    sb = ep / "storyboard.json"
    if sb.exists():
        p = subprocess.run([sys.executable, "-X", "utf8", str(root / "tests/check_storyboard.py"), str(sb)], cwd=root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        measures["check_storyboard"] = p.returncode == 0
        if p.returncode != 0:
            issues.append(issue("animation", "major", "розкадровка не проходить check_storyboard (повтори ракурсів/крупності): "
                                + (p.stdout + p.stderr).strip()[-300:], "storyboard", "check_storyboard: 0 помилок"))
    else:
        not_checked.append("storyboard.json немає — повтори кадрів не перевірено")

    # ---- H YouTube Readiness ----
    meta = youtube_meta(ep)
    if not meta:
        issues.append(issue("youtube", "major", "немає youtube.md (назва, опис, теги)", "edit", "youtube.md за зразком episode-017"))
    else:
        if not meta["titles"]:
            issues.append(issue("youtube", "major", "у youtube.md немає варіантів назви", "edit", "≥ 1 назва"))
        for t in meta["titles"]:
            if len(t) > 100:
                issues.append(issue("youtube", "major", f"назва {len(t)} знаків (> 100): «{t[:60]}…»", "edit", "назва ≤ 100 знаків"))
            if common.norm(t) in other_titles(root, ep):
                issues.append(issue("youtube", "major", f"назва повторює інший епізод: «{t}»", "edit", "унікальна назва"))
        if not meta["description"]:
            issues.append(issue("youtube", "major", "немає опису", "edit", "розділ «Опис»"))
        if not meta["tags"]:
            issues.append(issue("youtube", "minor", "немає тегів", "edit", "розділ «Теги»"))
        if not meta["materials"]:
            issues.append(issue("youtube", "major", "немає розділу «Використані матеріали» (реєстр джерел і ліцензій)", "edit",
                                "розділ із джерелами голосу, графіки, звуків, музики, шрифтів"))
        brand = [b for b in common.REAL_BRANDS
                 if re.search(rf"(?<!\w){re.escape(b)}(?!\w)", common.norm(" ".join(meta["titles"]) + " " + meta["description"]))]
        if brand:
            issues.append(issue("youtube", "major", f"реальні бренди в назві/описі: {brand}", "edit", "без реальних брендів"))
    cover = ep / "thumbnail" / "cover.jpg"
    if not cover.exists():
        issues.append(issue("youtube", "major", "немає thumbnail/cover.jpg", "thumbnail", "cover.jpg за еталоном"))
    elif cover.stat().st_size > 2_000_000:
        issues.append(issue("youtube", "major", "cover.jpg > 2 МБ (ліміт YouTube)", "thumbnail", "cover.jpg < 2 МБ"))

    not_checked.append("фотореалізм/стиль — лише огляд кадрів рецензентом (локального CLIP-детектора немає)")
    return {"issues": issues, "measures": measures, "not_checked": not_checked,
            "frames_dir": str(cv.frames_dir) if cv.frames_dir else None}


def _scripts_except(root: Path, ep: Path):
    for f in sorted((root / "episodes").glob("*/script.md"), key=lambda p: p.stat().st_mtime)[-10:]:
        if f.parent != ep:
            yield f, f.read_text(encoding="utf-8")


def aggregate(auto: dict, review: dict | None, weights: dict | None = None) -> dict:
    """Звести перевірки й рецензію в звіт: бали модулів, загальний бал, вердикт, критичні, задачі на виправлення."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    issues = list(auto["issues"]) + [dict(i, source="llm") for i in (review or {}).get("issues", [])]
    rv_scores = (review or {}).get("scores", {})
    modules = {}
    for m in MODULES:
        mine = [i for i in issues if i["module"] == m]
        det = max(0, 100 - sum(PENALTY.get(i["severity"], 0) for i in mine))
        llm = rv_scores.get(m)
        has_auto = m in ("animation", "audio", "editing", "youtube", "retention", "dialogue")   # A/E без рецензента — без бала
        if llm is None and not has_auto:
            score = None                                    # A/E без рецензента — немає даних
        elif llm is None:
            score = det
        else:
            score = round((det + max(0, min(100, float(llm)))) / 2)
        modules[m] = {"name": MODULES[m], "score": score, "weight": weights[m], "issues": mine}
    used = {m: v for m, v in modules.items() if v["score"] is not None}
    wsum = sum(v["weight"] for v in used.values()) or 1
    total = round(sum(v["score"] * v["weight"] for v in used.values()) / wsum, 1)
    critical = [f"{MODULES[i['module']]}: {i['msg']}" for i in issues if i["severity"] == "critical"]
    verdict = "BLOCKED_CRITICAL" if critical else verdict_for(total)
    fixes = [{"stage": i["stage"], "scope": i["scope"], "description": f"[{MODULES[i['module']]}] {i['msg']}",
              "criterion": i["criterion"], "severity": i["severity"]}
             for i in issues if i["severity"] in ("critical", "major")]
    if verdict in ("NEEDS_REVISION", "REJECTED") and not fixes:   # бал низький через дрібниці — виправляємо й їх
        fixes = [{"stage": i["stage"], "scope": i["scope"], "description": f"[{MODULES[i['module']]}] {i['msg']}",
                  "criterion": i["criterion"], "severity": i["severity"]} for i in issues]
    return {"score": total, "verdict": verdict, "critical": critical, "modules": modules, "fixes": fixes,
            "not_checked": auto.get("not_checked", []) + ([] if review else ["рецензія Claude (A Story, E Style) не виконувалась"]),
            "measures": auto.get("measures", {}), "review_summary": (review or {}).get("summary", "")}


def to_md(ep_name: str, rep: dict, rnd: int) -> str:
    out = [f"# QC — {ep_name} (раунд {rnd})", "",
           f"**Вердикт: {rep['verdict']}** · бал {rep['score']} / 100", ""]
    if rep["critical"]:
        out += ["## Критичні (блокують публікацію)", *[f"- {c}" for c in rep["critical"]], ""]
    out += ["## Модулі", "| Модуль | Бал | Вага | Проблеми |", "|---|---|---|---|"]
    for m in rep["modules"].values():
        out.append(f"| {m['name']} | {m['score'] if m['score'] is not None else '— (немає даних)'} | {m['weight']} | {len(m['issues'])} |")
    out.append("")
    for m in rep["modules"].values():
        if m["issues"]:
            out.append(f"### {m['name']}")
            out += [f"- **{i['severity']}** ({i['source']}, {i['scope']}): {i['msg']} → етап `{i['stage']}`; критерій: {i['criterion']}"
                    for i in m["issues"]]
            out.append("")
    if rep["fixes"]:
        out += ["## Задачі на виправлення", *[f"- `{f['stage']}` · {f['scope']}: {f['description']} — приймання: {f['criterion']}"
                                              for f in rep["fixes"]], ""]
    if rep.get("review_summary"):
        out += ["## Рецензія", rep["review_summary"], ""]
    out += ["## Не перевірено автоматично", *[f"- {n}" for n in rep["not_checked"]], ""]
    out += ["## Виміри", "```json", json.dumps(rep["measures"], ensure_ascii=False, indent=1)[:4000], "```"]
    return "\n".join(out) + "\n"

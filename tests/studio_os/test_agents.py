import json
import shutil
import subprocess

import pytest

from conftest import STUDIO
from studio_os.adapters import claude_code
from studio_os.adapters.youtube_data import YouTubeData, parse_iso_duration
from studio_os.agents import production, research
from studio_os.agents.base import BlockedError, RetryableError
from studio_os.core import queue, scheduler
from studio_os.orchestrator import Orchestrator

VID = "S20261123-1800"
LONG = "L20261124-1730"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    d = tmp_path / "fake_claude"
    d.mkdir()
    monkeypatch.setenv("STUDIO_CLAUDE_BIN", str(STUDIO / "tests" / "studio_os" / "fake_claude.py"))
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(d))

    class F:
        n = 0

        def push(self, structured=None, **kw):
            F.n += 1
            (d / f"resp-{F.n:03d}.json").write_text(json.dumps({"structured_output": structured, **kw}, ensure_ascii=False),
                                                     encoding="utf-8")

        def prompts(self):
            return [p.read_text(encoding="utf-8") for p in sorted(d.glob("prompt-*.txt"))]

        def args(self):
            return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("args-*.json"))]
    return F()


@pytest.fixture
def studio(cfg):
    """Мінімальна копія студії в tmp: скрипти, перевірки, канон-зразки."""
    r = cfg.root
    for rel in ("tests/check_storyboard.py", "scripts/storyboard_build.py", "scripts/voice_build.py", "scripts/pipeline.py",
                "channel/storyboard-rules.md", "channel/STYLE-GUIDE.md", "channel/palette.json",
                "channel/characters/ostap/ostap.json", "channel/locations/room-17.md",
                "episodes/episode-017/voice/voices.json", "episodes/episode-017/script.md"):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(STUDIO / rel, r / rel)
    return r


@pytest.fixture
def orch(con, cfg, fclock, studio):
    o = Orchestrator(con, cfg, "w")
    scheduler.plan(con, cfg, weeks=1)
    return o


def _st(con, stage, vid=VID):
    return con.execute("SELECT * FROM stages WHERE video_id=? AND stage=?", (vid, stage)).fetchone()


def _run_stage(o, stage, vid=VID):
    o.start_stage(vid, stage)
    r = o.work_once()
    return r


def _approve_through(o, con, vid, stages):
    for s in stages:
        if _st(con, s, vid)["status"] == "awaiting_approval":
            o.approve(vid, s)


# ---------- адаптери ----------
def test_claude_adapter_structured_and_errors(fake, tmp_path):
    fake.push({"ok": True})
    r = claude_code.ask("привіт", cwd=tmp_path, schema={"type": "object"})
    assert r.structured == {"ok": True}
    a = fake.args()[0]
    assert "--tools" in a and "--json-schema" in a and "--no-session-persistence" in a
    fake.push(None, is_error=True, result="Claude usage limit reached", returncode=1)
    with pytest.raises(RetryableError):
        claude_code.ask("x", cwd=tmp_path)
    fake.push(None, is_error=True, result="Please run /login to authenticate", returncode=1)
    with pytest.raises(BlockedError):
        claude_code.ask("x", cwd=tmp_path)


def test_claude_work_uses_restricted_tools(fake, tmp_path):
    fake.push({"summary": "ok"})
    claude_code.work("зроби", cwd=tmp_path)
    a = fake.args()[0]
    assert a[a.index("--permission-mode") + 1] == "acceptEdits"
    tools = a[a.index("--allowedTools") + 1]
    assert "PowerShell(node *),PowerShell(py -3.12 *)" in tools and "git" not in tools and "cd" not in tools


def test_youtube_adapter_cost_and_cache(tmp_path):
    calls = []

    def transport(url):
        calls.append(url)
        if "/search?" in url:
            return {"items": [{"id": {"videoId": "a"}}, {"id": {"videoId": "b"}}]}
        return {"items": []}
    spent = []
    yt = YouTubeData("KEY", tmp_path, transport=transport, on_cost=spent.append)
    assert yt.search("горор", published_after="2026-10-01T00:00:00Z") == ["a", "b"]
    assert yt.search("горор", published_after="2026-10-01T00:00:00Z") == ["a", "b"]  # кеш
    assert len(calls) == 1 and spent == [100]
    assert "key=KEY" in calls[0]
    assert not YouTubeData(None, tmp_path).available()
    assert parse_iso_duration("PT1M5S") == 65 and parse_iso_duration("PT12M") == 720


# ---------- research ----------
def _topics(n=3, evidence=("v1", "FAKE")):
    genres = ["rules", "rules", "tech", "zombie", "mystic"]
    return {"summary": "тренди", "topics": [
        {"title": f"Тема {i}", "description": "о", "why": "бо", "evidence": list(evidence), "plot_options": "п",
         "scores": {"reach": 20, "actuality": 99, "emotion": 15, "retention": 15, "originality": 15 - i,
                    "animation": 10, "fit": 10},
         "format": "both", "title_idea": f"Назва {i}", "hook": "гачок", "genre": genres[i]} for i in range(n)]}


def test_research_with_youtube_filters_fake_evidence(orch, con, fake, monkeypatch, cfg):
    monkeypatch.setenv("YOUTUBE_API_KEY", "KEY")

    def transport(url):
        if "/search?" in url:
            return {"items": [{"id": {"videoId": "v1"}}]}
        if "/videos?" in url:
            return {"items": [{"id": "v1", "snippet": {"title": "Правила нічної зміни", "channelId": "c1",
                                                       "channelTitle": "Ch", "publishedAt": "2026-11-13T09:00:00Z"},
                               "statistics": {"viewCount": "100000", "commentCount": "50"},
                               "contentDetails": {"duration": "PT58S"}}]}
        if "/channels?" in url:
            return {"items": [{"id": "c1", "statistics": {"subscriberCount": "10000"}}]}
        return {"items": [{"snippet": {"topLevelComment": {"snippet": {"textDisplay": "давай ще"}}}}]}
    real = research.YouTubeData
    monkeypatch.setattr(research, "YouTubeData", lambda *a, **k: real(*a, transport=transport, **{x: y for x, y in k.items() if x != "transport"}))
    fake.push(_topics())
    r = _run_stage(orch, "research")
    assert r["status"] == "done", r
    tv = con.execute("SELECT * FROM trend_videos").fetchone()
    assert tv["views_per_day"] == 10000.0 and tv["outlier"] == 10.0 and tv["format"] == "short"
    t = con.execute("SELECT * FROM topics ORDER BY id").fetchone()
    assert json.loads(t["evidence"]) == ["v1"]                      # «FAKE» відкинуто — доказ не з даних
    assert json.loads(t["scores"])["actuality"] == 15                # обрізано до максимуму критерію
    assert "100000 перегл." in fake.prompts()[0]


def test_research_without_key_marks_no_data(orch, con, fake, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    fake.push(_topics(evidence=("v1",)))
    assert _run_stage(orch, "research")["status"] == "done"
    rep = con.execute("SELECT * FROM trend_reports").fetchone()
    assert "немає YOUTUBE_API_KEY" in json.loads(rep["sources"])["youtube"]
    t = con.execute("SELECT * FROM topics").fetchone()
    assert json.loads(t["scores"])["actuality"] is None and json.loads(t["evidence"]) == []
    # наступне відео бере той самий звіт, без нового виклику Claude
    orch.approve(VID, "research")
    _run_stage(orch, "research", "S20261124-1800")
    assert len(fake.prompts()) == 1
    # власник додав ключ → звіт без даних не перевикористовується, збирається новий
    monkeypatch.setenv("YOUTUBE_API_KEY", "KEY")
    real = research.YouTubeData
    monkeypatch.setattr(research, "YouTubeData", lambda *a, **k: real(*a, transport=lambda url: {"items": []},
                                                                      **{x: y for x, y in k.items() if x != "transport"}))
    orch.approve("S20261124-1800", "research")
    fake.push(_topics(evidence=()))
    assert _run_stage(orch, "research", "S20261125-1800")["status"] == "done"
    assert len(fake.prompts()) == 2 and con.execute("SELECT COUNT(*) FROM trend_reports").fetchone()[0] == 2


# ---------- strategist ----------
def test_strategist_diversity_and_reject(orch, con, fake):
    fake.push(_topics(5, evidence=()))
    _run_stage(orch, "research"); orch.approve(VID, "research")
    _run_stage(orch, "planning")
    v = con.execute("SELECT topic FROM videos WHERE id=?", (VID,)).fetchone()
    assert v["topic"] == "Тема 0"                                      # найвищий бал
    orch.reject(VID, "planning", "не цікаво")
    _run_stage(orch, "planning")
    assert con.execute("SELECT topic FROM videos WHERE id=?", (VID,)).fetchone()["topic"] == "Тема 1"
    assert con.execute("SELECT status FROM topics WHERE title='Тема 0'").fetchone()["status"] == "rejected"
    orch.approve(VID, "planning")
    # наступний Short: «Тема 2» (tech) обходить «rules», бо rules щойно був
    for s in ("research",):
        _run_stage(orch, s, "S20261124-1800"); orch.approve("S20261124-1800", s)
    _run_stage(orch, "planning", "S20261124-1800")
    assert con.execute("SELECT topic FROM videos WHERE id='S20261124-1800'").fetchone()["topic"] == "Тема 2"


# ---------- scriptwriter ----------
def _script(stock=False, words=140):
    text = ("Слово " * (words - 2)).strip() + (" і тоді я зрозумів." if stock else " кінець.")
    return {"slug": "dzvinok z domu", "title": "ДЗВІНОК", "logline": "л", "genre": "tech", "hero": "ostap",
            "locations": ["room-17"], "new_assets": [], "script_md": "# Епізод — «ДЗВІНОК» (Shorts)\n\nСтатус: SCRIPT",
            "voice": [{"block": "NR", "speaker": "Диктор", "voice_key": "NR",
                       "lines": [{"id": "NR-01", "text": "[tense] " + text, "pre": 0.5, "filter": "CLEAN"}]}]}


def _to_script(orch, con, fake):
    fake.push(_topics(3, evidence=()))
    for s in ("research", "planning"):
        _run_stage(orch, s); orch.approve(VID, s)


def test_scriptwriter_writes_files_and_retries_on_repeated_stock(orch, con, fake, studio):
    (studio / "episodes/episode-017/script.md").write_text("…і тоді я зрозумів…", encoding="utf-8")
    _to_script(orch, con, fake)
    fake.push(_script(stock=True))      # штамп уже був у попередньому епізоді → переробка
    fake.push(_script())
    r = _run_stage(orch, "script")
    assert r["status"] == "done", r
    assert "штамп" in fake.prompts()[-1] or "Виправ" in fake.prompts()[-1]
    ep = studio / "episodes" / "episode-018"
    assert (ep / "script.md").exists()
    plan = json.loads((ep / "voice/voice-plan.json").read_text(encoding="utf-8"))
    assert plan["order"] == [{"id": "NR-01", "filter": "CLEAN", "pre": 0.5}]
    req = json.loads((ep / "voice/requests.json").read_text(encoding="utf-8"))
    assert req["requests"][0]["ids"] == ["NR-01"]
    assert req["requests"][0]["name"] == "01-NR"                       # унікальні назви: аудіо зберігається за назвою
    assert json.loads((ep / "voice/voices.json").read_text(encoding="utf-8"))["NR"]["voice_id"]
    assert con.execute("SELECT episode_dir FROM videos WHERE id=?", (VID,)).fetchone()[0] == "episodes/episode-018"
    assert _st(con, "script")["status"] == "awaiting_approval"


def test_scriptwriter_blocks_after_failed_rework(orch, con, fake):
    orch.cfg.production["strict_checks"] = True              # суворий режим
    _to_script(orch, con, fake)
    fake.push(_script(words=20))   # закоротко для Shorts
    fake.push(_script(words=20))
    r = _run_stage(orch, "script")
    assert r["status"] == "dead" and "хронометраж" in _st(con, "script")["note"]


# ---------- voice (без мережі: аудіо вже згенеровано) ----------
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="потрібен ffmpeg")
def test_voice_builds_from_existing_lines(orch, con, fake, studio):
    _to_script(orch, con, fake)
    fake.push(_script())
    _run_stage(orch, "script"); orch.approve(VID, "script")
    ep = studio / "episodes" / "episode-018"
    (ep / "audio/lines").mkdir(parents=True)
    (ep / "audio/raw").mkdir()
    (ep / "audio/raw/01-NR.json").write_text("{}", encoding="utf-8")   # «уже згенеровано» → без запиту до ElevenLabs
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=3", "-ar", "44100",
                    str(ep / "audio/lines/NR-01.mp3")], check=True)
    r = _run_stage(orch, "voice")
    assert r["status"] == "done", con.execute("SELECT last_error FROM jobs ORDER BY id DESC").fetchone()[0]
    lines = json.loads((ep / "audio/voice-lines.json").read_text(encoding="utf-8"))
    assert lines[0]["id"] == "NR-01" and lines[0]["start"] > 0


def test_scriptwriter_rejects_non_narrator_voice(orch, con, cfg, fake, studio):
    """narrator_only = true: увесь текст — диктор True Crime; другий голос → переробка, потім людина."""
    cfg.production["narrator_only"] = True
    _to_script(orch, con, fake)
    s = _script()
    s["voice"].append({"block": "XX", "speaker": "Новий", "voice_key": "XX",
                       "lines": [{"id": "XX-01", "text": "Привіт.", "pre": 0, "filter": "PHONE"}]})
    fake.push(s); fake.push(s)
    assert _run_stage(orch, "script")["status"] == "dead"
    assert "XX" in _st(con, "script")["note"]


def test_request_names_unique_even_for_same_block(tmp_path, cfg):
    from studio_os.agents import scriptwriter
    s = _script()
    s["voice"] = [{"block": "01-hook", "speaker": "Д", "voice_key": "NR", "lines": [{"id": "NR-01", "text": "А.", "pre": 0, "filter": "CLEAN"}]},
                  {"block": "01-hook", "speaker": "М", "voice_key": "MN", "lines": [{"id": "MN-01", "text": "Б.", "pre": 0, "filter": "CLOSE"}]},
                  {"block": "02", "speaker": "Д", "voice_key": "NR", "lines": [{"id": "NR-02", "text": "В.", "pre": 0, "filter": "CLEAN"}]}]
    ep = cfg.root / "episodes/episode-555"
    ep.mkdir(parents=True)
    scriptwriter.write_files(ep, s, cfg)
    names = [r["name"] for r in json.loads((ep / "voice/requests.json").read_text(encoding="utf-8"))["requests"]]
    assert names == ["01-NR", "02-MN", "03-NR"]
    s["voice"][0]["lines"][0]["seq"], s["voice"][1]["lines"][0]["seq"], s["voice"][2]["lines"][0]["seq"] = 3, 1, 2
    scriptwriter.write_files(ep, s, cfg)                                 # згруповано за голосом → порядок за seq
    plan = json.loads((ep / "voice/voice-plan.json").read_text(encoding="utf-8"))
    assert [o["id"] for o in plan["order"]] == ["MN-01", "NR-02", "NR-01"]
    assert "_seq" not in plan["order"][0]
    bad = _script()
    bad["voice"][0]["lines"][0]["seq"] = 5
    assert any("seq" in e for e in scriptwriter.check(bad, cfg, "short", [])["errors"])


def test_voice_blocks_on_uncast_voice(orch, con, fake, studio):
    """narrator_only = false (власник): персонажі можуть мати голоси, але новий голос — лише після кастингу власника."""
    _to_script(orch, con, fake)
    s = _script()
    s["voice"].append({"block": "XX", "speaker": "Новий", "voice_key": "XX",
                       "lines": [{"id": "XX-01", "text": "Привіт.", "pre": 0, "filter": "PHONE"}]})
    fake.push(s)
    assert _run_stage(orch, "script")["status"] == "done"
    orch.approve(VID, "script")
    assert _run_stage(orch, "voice")["status"] == "dead"
    assert "кастинг" in _st(con, "voice")["note"]


def test_narrator_pinned_to_true_crime(cfg, studio):
    """Старі епізоди мали інший голос NR — каталог усе одно віддає голос із config (True Crime)."""
    from studio_os.agents import common
    old = studio / "episodes/episode-004/voice"
    old.mkdir(parents=True, exist_ok=True)
    (old / "voices.json").write_text(json.dumps({"NR": {"voice_id": "OLDVOICE", "name": "Інший диктор"}}), encoding="utf-8")
    v = common.voices(studio, cfg)
    assert v["NR"] == {"voice_id": "tZssYepgGaQmegsMEXjK", "name": "True - Crime & Horror Narrator"}
    cfg.production["narrator_only"] = True
    assert list(common.voices(studio, cfg)) == ["NR"]
    assert cfg.production["words_per_minute"] == 140


def test_voice_blocks_on_wrong_narrator_voice(orch, con, fake, studio):
    _to_script(orch, con, fake)
    fake.push(_script())
    _run_stage(orch, "script"); orch.approve(VID, "script")
    vj = studio / "episodes/episode-018/voice/voices.json"
    assert json.loads(vj.read_text(encoding="utf-8"))["NR"]["voice_id"] == "tZssYepgGaQmegsMEXjK"
    vj.write_text(json.dumps({"NR": {"voice_id": "OLDVOICE", "name": "Інший диктор"}}), encoding="utf-8")
    assert _run_stage(orch, "voice")["status"] == "dead"
    assert "True - Crime" in _st(con, "voice")["note"]


# ---------- storyboard ----------
def _board(i, angle, size, end):
    return {"id": f"A0{i}", "scene": "S01", "angle": angle, "size": size, "camera": f"c{i}", "location": "room-17",
            "end_voice": end, "subject": "кадр", "beats": "рух", "tech": "2d"}


def _sb(boards):
    return {"title": "Т", "notes": "", "tail": 1.0, "shift": {}, "scenes": {"S01": "Хук"}, "speakers": {"NR": "Диктор"},
            "boards": boards}


def test_scriptwriter_fast_mode_lint_is_warning_not_rework(orch, con, fake):
    orch.cfg.production["strict_checks"] = False             # швидкий режим (власник, 2026-11-24)
    _to_script(orch, con, fake)
    fake.push(_script(words=20))                             # закоротко — але це не структурна помилка
    r = _run_stage(orch, "script")
    assert r["status"] == "done" and len(fake.prompts()) == 2  # research + один сценарій, без переробки
    lint = json.loads(_st(con, "script")["result"])["lint"]
    assert any("хронометраж" in w for w in lint["warnings"])


def test_storyboard_retries_until_check_passes(orch, con, fake, studio):
    orch.cfg.production["strict_checks"] = True              # суворий режим
    con.execute("UPDATE videos SET episode_dir='episodes/episode-777', topic='т' WHERE id=?", (VID,))
    ep = studio / "episodes/episode-777"
    (ep / "audio").mkdir(parents=True)
    (ep / "script.md").write_text("# Епізод — Т\nroom-17", encoding="utf-8")
    (ep / "audio/voice-lines.json").write_text(json.dumps(
        [{"id": "NR-01", "start": 0.5, "end": 3.0, "text": "а"}, {"id": "NR-02", "start": 3.3, "end": 6.0, "text": "б"}]),
        encoding="utf-8")
    for s in ("research", "planning", "script", "voice"):
        con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage=?", (VID, s))
    fake.push(_sb([_board(1, "front", "MS", 3.0), _board(2, "front", "MS", 6.0)]))      # R1: однакові ракурс і крупність
    fake.push(_sb([_board(1, "front", "MS", 3.0), _board(2, "low", "CU", 6.0)]))
    r = _run_stage(orch, "storyboard")
    assert r["status"] == "done", r
    assert "R1" in fake.prompts()[1]
    sb = json.loads((ep / "storyboard.json").read_text(encoding="utf-8"))
    assert [b["id"] for b in sb["boards"]] == ["A01", "A02"] and sb["boards"][-1]["dur"] == pytest.approx(4.0)


# ---------- агентні сесії ----------
def _ready_for(con, stage, ep="episodes/episode-777", vid=VID):
    con.execute("UPDATE videos SET episode_dir=?, topic='т' WHERE id=?", (ep, vid))
    for row in con.execute("SELECT stage, seq FROM stages WHERE video_id=? ORDER BY seq", (vid,)).fetchall():
        if row["stage"] == stage:
            break
        con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage=?", (vid, row["stage"]))


def test_canon_guard_blocks_session_that_changed_palette(orch, con, fake, studio):
    _ready_for(con, "thumbnail")
    (studio / "episodes/episode-777").mkdir(parents=True)
    fake.push({"summary": "ok", "files": [], "compositions": [], "checks": [], "blockers": []},
              write={"channel/palette.json": "{}", "episodes/episode-777/thumbnail/cover-notext.png": "x"})
    r = _run_stage(orch, "thumbnail")
    assert r["status"] == "dead"
    assert "змінила канон" in _st(con, "thumbnail")["note"] and "palette.json" in _st(con, "thumbnail")["note"]


def test_thumbnail_long_waits_for_owner_chatgpt_file(orch, con, fake, studio):
    orch.set_mode("semi")
    _ready_for(con, "thumbnail", ep="episodes/long-episode-x", vid=LONG)
    ep = studio / "episodes/long-episode-x"
    ep.mkdir(parents=True)
    fake.push({"summary": "чорнова", "files": [], "compositions": [], "checks": [], "blockers": []},
              write={"episodes/long-episode-x/thumbnail/cover-notext.png": "x",
                     "episodes/long-episode-x/thumbnail/gpt-prompt.md": "промпт"})
    orch.advance(only=LONG)
    assert orch.work_once()["status"] == "dead"
    st = _st(con, "thumbnail", LONG)
    assert st["status"] == "needs_human" and "cover-gpt" in st["note"]
    assert orch.advance(only=LONG) == []                   # файлу ще немає — чекаємо, нічого не запускаємо
    (ep / "thumbnail/cover-gpt.webp").write_text("x")      # власник поклав файл (будь-яке розширення)
    assert orch.advance(only=LONG)                         # етап продовжено автоматично


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="потрібен ffmpeg")
def test_thumbnail_short_no_chatgpt_step(orch, con, fake, studio):
    """Shorts (власник, 2026-11-23): обкладинка одразу з текстом за еталоном v4, без очікування ChatGPT."""
    orch.set_mode("semi")
    _ready_for(con, "thumbnail")
    th = studio / "episodes/episode-777/thumbnail"
    th.mkdir(parents=True)
    for name, size in (("cover.jpg", "1080x1920"), ("small.png", "360x640")):   # те, що відрендерила б сесія
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={size}", "-frames:v", "1",
                        str(th / name)], check=True)
    fake.push({"summary": "ok", "files": [], "compositions": ["Ep777Thumb"], "checks": [], "blockers": []})
    orch.advance(only=VID)
    r = orch.work_once()
    assert r["status"] == "done", r
    prompt = fake.prompts()[-1]
    assert "Ep777Thumb" in prompt and "ThumbText" in prompt and "ChatGPT" in prompt   # «БЕЗ кроку ChatGPT»
    assert "WAIT" not in (_st(con, "thumbnail")["note"] or "")


def test_animation_rejects_unregistered_compositions(orch, con, fake, studio):
    _ready_for(con, "animation", ep="long-episode-x".join(["episodes/", ""]))
    ep = studio / "episodes/long-episode-x"
    ep.mkdir(parents=True)
    (ep / "storyboard.json").write_text(json.dumps({"boards": [{"scene": "S01"}]}), encoding="utf-8")
    (studio / "video/src").mkdir(parents=True)
    (studio / "video/src/Root.tsx").write_text("<Composition id=\"Old\" />", encoding="utf-8")
    fake.push({"summary": "ok", "files": [], "compositions": ["LxShotA01"], "checks": [], "blockers": []})
    r = _run_stage(orch, "animation")
    assert r["status"] == "queued" and "Root.tsx" in r["error"]   # звітові сесії не віримо на слово


def test_composition_check_uses_real_remotion_list(monkeypatch, studio):
    """Композиції з циклу (`id={`E19Shot${b.id}`}`) не видно в тексті Root.tsx — беремо справжній список Remotion."""
    from studio_os.agents import agentic
    (studio / "video/src").mkdir(parents=True)
    (studio / "video/src/Root.tsx").write_text("{B.map((b) => <Composition id={`E19Shot${b.id}`} />)}", encoding="utf-8")
    monkeypatch.setattr(agentic, "registered_compositions", lambda root: {"E19ShotA01", "Ep019"})
    assert agentic.missing_compositions(studio, ["E19ShotA01", "E19ShotZ99"]) == ["E19ShotZ99"]
    assert agentic.missing_compositions(studio, ["Ep019 (existing; old edit.json)"]) == []   # коментар сесії
    monkeypatch.setattr(agentic, "registered_compositions", lambda root: None)   # Remotion недоступний → текст
    assert agentic.missing_compositions(studio, ["E19ShotA01"]) == ["E19ShotA01"]


def test_remotion_temp_removes_only_new_bundles(tmp_path, monkeypatch):
    from studio_os.agents import agentic
    monkeypatch.setattr(agentic.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "remotion-webpack-bundle-old"; old.mkdir()
    monkeypatch.setattr(agentic, "remotion_busy", lambda: False)
    with agentic.remotion_temp():
        (tmp_path / "remotion-webpack-bundle-new").mkdir()
    assert old.exists() and not (tmp_path / "remotion-webpack-bundle-new").exists()   # чуже не чіпаємо
    monkeypatch.setattr(agentic, "remotion_busy", lambda: True)                       # працює рендер — не чіпаємо
    with agentic.remotion_temp():
        (tmp_path / "remotion-webpack-bundle-new2").mkdir()
    assert (tmp_path / "remotion-webpack-bundle-new2").exists()


def test_session_blockers_go_to_human(orch, con, fake, studio):
    _ready_for(con, "animation", ep="episodes/long-episode-x")
    ep = studio / "episodes/long-episode-x"
    ep.mkdir(parents=True)
    (ep / "storyboard.json").write_text(json.dumps({"boards": [{"scene": "S01"}]}), encoding="utf-8")
    fake.push({"summary": "", "files": [], "compositions": [], "checks": [],
               "blockers": ["немає ригу персонажа «сусід» — потрібне погодження нового персонажа"]})
    assert _run_stage(orch, "animation")["status"] == "dead"
    assert "сусід" in _st(con, "animation")["note"]


def test_owner_notes_do_not_stop_stage(orch, con, fake, studio):
    _ready_for(con, "animation", ep="episodes/long-episode-x")
    ep = studio / "episodes/long-episode-x"
    ep.mkdir(parents=True)
    (ep / "storyboard.json").write_text(json.dumps({"boards": [{"scene": "S01"}]}), encoding="utf-8")
    (studio / "video/src").mkdir(parents=True)
    (studio / "video/src/Root.tsx").write_text("<Composition id=\"LxShotA01\" />", encoding="utf-8")
    fake.push({"summary": "ok", "files": [], "compositions": ["LxShotA01"], "checks": [], "blockers": [],
               "owner_notes": ["Не блокер: глянути край плану B03"]})
    assert _run_stage(orch, "animation")["status"] == "done"
    ev = con.execute("SELECT level, message FROM events WHERE message LIKE '%для власниці%'").fetchone()
    assert ev["level"] == "warning" and "B03" in ev["message"]


def test_wait_prefix_constant_matches_orchestrator():
    assert production.WAIT_PREFIX == "WAIT_FILE:"


# ---------- ліміт підписки Claude ----------
def test_reset_time_parsing():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 23, 20, 49, tzinfo=timezone.utc)
    assert claude_code.reset_time("You've hit your 5-hour limit · resets 2am (Europe/Kyiv)", now) == \
        datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
    assert claude_code.reset_time("rate limit", now) is None


LIMIT_MSG = "You've hit your 5-hour limit · resets 2am (Europe/Kyiv)"


def test_limit_raises_limit_error(fake, studio):
    from studio_os.errors import LimitError
    fake.push(None, is_error=True, result=LIMIT_MSG, returncode=1)
    with pytest.raises(LimitError) as e:
        claude_code.ask("x", cwd=studio)
    assert e.value.until is not None


def test_limit_defers_without_spending_attempts_and_keeps_feedback(orch, con, fake, studio):
    orch.reject(VID, "research", "більше зомбі")                     # примітка власника до етапу
    for _ in range(3):                                                # тричі поспіль ліміт — спроби не згоряють
        fake.push(None, is_error=True, result=LIMIT_MSG, returncode=1)
    fake.push(_topics(3, evidence=()))
    jid = orch.start_stage(VID, "research")
    for _ in range(3):
        r = orch.work_once()
        assert r["status"] == "deferred_limit" and r["until"] == "2026-11-24T00:00:00Z", r
        job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        assert job["attempts"] == 0 and job["status"] == "queued"
        assert _st(con, "research")["note"] == "доопрацювати: більше зомбі"
        assert orch.work_once() is None                               # до скидання задача не береться
        con.execute("UPDATE jobs SET run_after='2026-11-23T00:00:00Z' WHERE id=?", (jid,))   # «настав час скидання»
    assert orch.work_once()["status"] == "done"
    assert con.execute("SELECT attempts FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 1   # зараховано лише успішну


def test_retry_keeps_owner_feedback_note(orch, con, fake):
    orch.reject(VID, "research", "інший жанр")
    fake.push(None, is_error=True, result="мережа впала", returncode=1)
    orch.start_stage(VID, "research")
    assert orch.work_once()["status"] == "queued"
    assert _st(con, "research")["note"] == "доопрацювати: інший жанр"

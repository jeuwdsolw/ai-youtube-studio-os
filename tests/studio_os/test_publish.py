"""Етап 5: публікація YouTube — підставний HTTP (жодних справжніх запитів), private + publishAt, ідемпотентність,
продовження перерваного завантаження, перевірка каналу, обмін слотів."""
import json
import threading
import urllib.parse
import urllib.request

import pytest

from studio_os.adapters import youtube_upload as yu
from studio_os.agents import publish
from studio_os.agents.base import BlockedError, RetryableError
from studio_os.core import scheduler
from studio_os.orchestrator import Orchestrator

VID = "S20261123-1800"          # 18:00 Київ = 16:00Z; фальшивий годинник — 09:00Z
YOUTUBE_MD = """# YouTube — тест

## Три варіанти назви
1. Друга ночі. Стукає той, кого ти поховала #shorts
2. Інша назва

## Опис
Опис <без> тегів.

## Теги
страшилка, горор, shorts українською

## Налаштування завантаження
- Аудиторія: «Не для дітей» (горор).
- Категорія: Фільми й анімація. Мова відео: українська.
- Змінений/синтетичний вміст: **Ні** — анімація.
"""


class FakeYT:
    """Мінімальна імітація OAuth + YouTube Data API: resumable upload частинами по 308."""

    def __init__(self, channel="UCowner", fail_after_chunks=None):
        self.channel, self.calls, self.received, self.meta = channel, [], 0, None
        self.fail_after = fail_after_chunks
        self.chunks = 0
        self.publish_at = None

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split("?")[0]))
        if url == yu.TOKEN_URL:
            q = urllib.parse.parse_qs(body.decode())
            assert q["grant_type"][0] in ("refresh_token", "authorization_code")
            return 200, {}, json.dumps({"access_token": "AT", "expires_in": 3600, "refresh_token": "RT"}).encode()
        assert headers.get("Authorization") == "Bearer AT"
        if url.startswith(yu.API + "channels"):
            return 200, {}, json.dumps({"items": [{"id": self.channel, "snippet": {"title": "Правила виживання"}}]}).encode()
        if url.startswith(yu.UPLOAD + "videos"):
            self.meta = json.loads(body)
            self.publish_at = self.meta["status"]["publishAt"]
            return 200, {"location": "https://upload.example/session/1"}, b""
        if url == "https://upload.example/session/1":
            total = int(headers["Content-Range"].rsplit("/", 1)[1])
            if headers["Content-Range"].startswith("bytes */"):
                return (308, {"range": f"bytes=0-{self.received - 1}"} if self.received else {}, b"")
            if self.fail_after is not None and self.chunks >= self.fail_after:
                self.fail_after = None
                raise RetryableError("обрив мережі")
            self.chunks += 1
            self.received += len(body)
            if self.received >= total:
                return 200, {}, json.dumps({"id": "yt123"}).encode()
            return 308, {"range": f"bytes=0-{self.received - 1}"}, b""
        if url.startswith(yu.UPLOAD + "thumbnails/set"):
            return 200, {}, b"{}"
        if url.startswith(yu.UPLOAD + "captions"):
            assert b"multipart" not in body[:10] and b'"language": "uk"' in body
            return 200, {}, b"{}"
        if url.startswith(yu.API + "videos"):
            return 200, {}, json.dumps({"items": [{"id": "yt123", "status": {"privacyStatus": "private",
                                                                              "publishAt": self.publish_at}}]}).encode()
        raise AssertionError(f"неочікуваний запит {method} {url}")


@pytest.fixture
def ready(con, cfg, fclock, monkeypatch):
    o = Orchestrator(con, cfg, "w")
    scheduler.plan(con, cfg, weeks=1)
    ep = cfg.root / "episodes/episode-777"
    (ep / "renders").mkdir(parents=True)
    (ep / "renders/final_master.mp4").write_bytes(b"\0" * (yu.CHUNK * 2 + 123))
    (ep / "thumbnail").mkdir()
    (ep / "thumbnail/cover.jpg").write_bytes(b"jpg")
    (ep / "subtitles").mkdir()
    (ep / "subtitles/episode-777.uk.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nТест\n", encoding="utf-8")
    (ep / "youtube.md").write_text(YOUTUBE_MD, encoding="utf-8")
    con.execute("UPDATE videos SET episode_dir='episodes/episode-777', topic='т' WHERE id=?", (VID,))
    for s in ("research", "planning", "script", "voice", "storyboard", "animation", "edit", "thumbnail"):
        con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage=?", (VID, s))
    con.execute("UPDATE stages SET status='done', result=? WHERE video_id=? AND stage='qc'",
                (json.dumps({"verdict": "NEEDS_REVISION", "critical": []}), VID))
    con.execute("UPDATE stages SET status='done', approved_by='owner' WHERE video_id=? AND stage='approval'", (VID,))
    con.commit()
    yd = cfg.root / "data/youtube"
    yd.mkdir(parents=True)
    (yd / "client_secret.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    (yd / "token.json").write_text(json.dumps({"refresh_token": "RT", "channel": {"id": "UCowner", "title": "Правила"}}))
    fake = FakeYT()
    monkeypatch.setattr(yu, "_http", fake)
    return o, fake


def _stage(con, s="publish"):
    return con.execute("SELECT * FROM stages WHERE video_id=? AND stage=?", (VID, s)).fetchone()


def test_build_meta_requires_owner_decision_on_synthetic(tmp_path):
    (tmp_path / "youtube.md").write_text(YOUTUBE_MD.replace("**Ні** — анімація", "**уточнити у власниці**"), encoding="utf-8")
    with pytest.raises(BlockedError, match="синтетичний"):
        publish.build_meta(tmp_path, "short", "2026-11-25T16:00:00Z", {})
    (tmp_path / "youtube.md").write_text(YOUTUBE_MD, encoding="utf-8")
    m = publish.build_meta(tmp_path, "short", "2026-11-25T16:00:00Z", {})
    assert m["status"] == {"privacyStatus": "private", "publishAt": "2026-11-25T16:00:00Z", "embeddable": True,
                           "license": "youtube", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}
    assert m["snippet"]["title"].startswith("Друга ночі") and "<" not in m["snippet"]["description"]
    assert m["snippet"]["categoryId"] == "1" and m["snippet"]["tags"] == ["страшилка", "горор", "shorts українською"]


def test_publish_schedules_private_with_publish_at(ready, con):
    o, fake = ready
    o.start_stage(VID, "publish")
    r = o.work_once()
    assert r["status"] == "done" and "yt123" in r["summary"]
    assert fake.meta["status"]["publishAt"] == "2026-11-23T16:00:00Z"       # час слоту, а не «зараз»
    up = con.execute("SELECT * FROM uploads WHERE video_id=?", (VID,)).fetchone()
    assert up["yt_video_id"] == "yt123" and up["thumbnail"] == "ok" and up["captions"] == "ok"
    assert con.execute("SELECT status FROM videos WHERE id=?", (VID,)).fetchone()[0] == "published"
    assert sum(1 for m, u in fake.calls if u == yu.UPLOAD + "videos") == 1


def test_interrupted_upload_resumes_without_duplicate(ready, con, fclock):
    o, fake = ready
    fake.fail_after = 1                      # другий шматок обривається
    o.start_stage(VID, "publish")
    assert o.work_once()["status"] == "queued"
    up = con.execute("SELECT * FROM uploads WHERE video_id=?", (VID,)).fetchone()
    assert up["session_uri"] and not up["yt_video_id"]
    con.execute("UPDATE jobs SET run_after='2000-01-01T00:00:00Z'"); con.commit()
    assert o.work_once()["status"] == "done"
    assert sum(1 for m, u in fake.calls if u == yu.UPLOAD + "videos") == 1   # одна сесія — одне відео
    assert fake.received == yu.CHUNK * 2 + 123                               # продовжило з місця обриву


def test_wrong_channel_blocks(ready, con):
    o, fake = ready
    fake.channel = "UCsomeoneelse"
    o.start_stage(VID, "publish")
    assert o.work_once()["status"] == "dead"
    assert "іншому каналу" in _stage(con)["note"]
    assert not any(u == yu.UPLOAD + "videos" for _, u in fake.calls)


def test_past_slot_is_not_published(ready, con, fclock):
    o, fake = ready
    fclock.t = fclock.t.replace(hour=15, minute=50)    # 10 хв до слоту
    o.start_stage(VID, "publish")
    assert o.work_once()["status"] == "dead"
    assert "минув або надто близько" in _stage(con)["note"]


def test_authorize_loopback_pkce(cfg, monkeypatch):
    fake = FakeYT()
    auth = yu.YouTubeAuth(cfg.root / "data/youtube", http=fake)
    (cfg.root / "data/youtube").mkdir(parents=True)
    (cfg.root / "data/youtube/client_secret.json").write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))

    def browser(url):                         # «власниця натиснула Дозволити»: Google перенаправляє на loopback
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and "yt-analytics.readonly" in q["scope"][0]
        back = q["redirect_uri"][0] + "/?" + urllib.parse.urlencode({"code": "C", "state": q["state"][0]})
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=10).read(), daemon=True).start()

    tok = auth.authorize(open_browser=browser, timeout_s=20)
    assert tok["refresh_token"] == "RT"
    assert json.loads((cfg.root / "data/youtube/token.json").read_text())["refresh_token"] == "RT"
    auth.save_channel({"id": "UCowner", "title": "Правила"})
    assert auth.token()["channel"]["id"] == "UCowner"


def test_swap_slots_moves_history_and_keeps_deadlines(con, cfg, fclock):
    o = Orchestrator(con, cfg, "w")
    scheduler.plan(con, cfg, weeks=2)
    a, b = "S20261124-1800", "S20261130-1800"
    con.execute("UPDATE videos SET episode_dir='episodes/episode-019', topic='Стук', status='in_production' WHERE id=?", (b,))
    con.execute("UPDATE stages SET status='done' WHERE video_id=? AND stage='script'", (b,))
    con.execute("INSERT INTO qc_reports(video_id, round, verdict, score, critical, report, created_at) "
                "VALUES (?, 1, 'NEEDS_REVISION', 76, 0, '{}', 'now')", (b,))
    con.commit()
    dl_a = con.execute("SELECT deadline_utc FROM stages WHERE video_id=? AND stage='edit'", (a,)).fetchone()[0]
    o.swap_slots(a, b)
    va = con.execute("SELECT * FROM videos WHERE id=?", (a,)).fetchone()
    assert va["episode_dir"] == "episodes/episode-019" and va["topic"] == "Стук" and va["status"] == "in_production"
    assert con.execute("SELECT status FROM stages WHERE video_id=? AND stage='script'", (a,)).fetchone()[0] == "done"
    assert con.execute("SELECT COUNT(*) FROM qc_reports WHERE video_id=?", (a,)).fetchone()[0] == 1
    assert con.execute("SELECT episode_dir FROM videos WHERE id=?", (b,)).fetchone()[0] is None
    assert con.execute("SELECT deadline_utc FROM stages WHERE video_id=? AND stage='edit'", (a,)).fetchone()[0] == dl_a


def test_manual_start_cannot_bypass_qc(ready, con):
    o, fake = ready
    con.execute("UPDATE stages SET result=? WHERE video_id=? AND stage='qc'",
                (json.dumps({"verdict": "BLOCKED_CRITICAL", "critical": ["чорний екран"]}), VID))
    with pytest.raises(ValueError, match="QC"):
        o.start_stage(VID, "publish")
    assert not fake.calls


def test_client_secret_from_env_when_json_has_none(cfg, monkeypatch):
    d = cfg.root / "data/youtube"
    d.mkdir(parents=True)
    (d / "client_secret.json").write_text(json.dumps({"installed": {"client_id": "cid"}}))
    auth = yu.YouTubeAuth(d, http=FakeYT())
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    with pytest.raises(BlockedError, match="YOUTUBE_CLIENT_SECRET"):
        auth.client()
    (cfg.root / ".env").write_text("YOUTUBE_CLIENT_SECRET=s3cr3t", encoding="utf-8")
    assert auth.client()["client_secret"] == "s3cr3t"

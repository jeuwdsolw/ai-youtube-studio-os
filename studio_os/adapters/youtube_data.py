"""YouTube Data API v3 (публічні дані) — лише стандартна бібліотека.

Потрібен ключ YOUTUBE_API_KEY у .env (Google Cloud → APIs & Services → Credentials → API key,
з увімкненим «YouTube Data API v3»). Без ключа адаптер не вигадує дані — повертає `available() == False`.

Вартість квоти (документація: developers.google.com/youtube/v3/determine_quota_cost):
search.list = 100, videos.list / channels.list / commentThreads.list = 1. Денна квота за замовчуванням — 10 000.
Відповіді кешуються у data/cache/youtube (TTL), щоб не витрачати квоту повторно.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from ..errors import BlockedError, RetryableError

BASE = "https://www.googleapis.com/youtube/v3/"
COST = {"search": 100, "videos": 1, "channels": 1, "commentThreads": 1}
Transport = Callable[[str], dict]


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "studio-os/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        reason = ""
        try:
            reason = json.loads(body)["error"]["errors"][0]["reason"]
        except (KeyError, IndexError, json.JSONDecodeError):
            pass
        if e.code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise RetryableError(f"YouTube API: квоту вичерпано ({reason})") from e
        if e.code in (400, 403):
            raise BlockedError(f"YouTube API {e.code} {reason}: перевір ключ YOUTUBE_API_KEY і чи увімкнено API") from e
        raise RetryableError(f"YouTube API HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RetryableError(f"YouTube API недоступний: {e}") from e


class YouTubeData:
    def __init__(self, key: str | None, cache_dir: Path, *, ttl_s: int = 6 * 3600,
                 transport: Transport | None = None, on_cost: Callable[[int], None] | None = None):
        self.key = key
        self.cache_dir = cache_dir
        self.ttl_s = ttl_s
        self.transport = transport or _http_get
        self.on_cost = on_cost or (lambda units: None)
        self.units = 0

    def available(self) -> bool:
        return bool(self.key)

    def _get(self, endpoint: str, **params) -> dict:
        if not self.key:
            raise BlockedError("немає YOUTUBE_API_KEY у .env")
        params = {k: v for k, v in params.items() if v is not None}
        ck = hashlib.sha256(json.dumps([endpoint, sorted(params.items())], ensure_ascii=False).encode()).hexdigest()[:24]
        cf = self.cache_dir / f"{endpoint}-{ck}.json"
        if cf.exists() and time.time() - cf.stat().st_mtime < self.ttl_s:
            return json.loads(cf.read_text(encoding="utf-8"))
        url = BASE + endpoint + "?" + urllib.parse.urlencode({**params, "key": self.key})
        data = self.transport(url)
        cost = COST.get(endpoint, 1)
        self.units += cost
        self.on_cost(cost)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def search(self, q: str, *, published_after: str, max_results: int = 25, order: str = "viewCount",
               region: str | None = None, language: str | None = None, duration: str | None = None) -> list[str]:
        d = self._get("search", part="snippet", q=q, type="video", order=order, maxResults=max_results,
                      publishedAfter=published_after, regionCode=region, relevanceLanguage=language,
                      videoDuration=duration)
        return [it["id"]["videoId"] for it in d.get("items", []) if it.get("id", {}).get("videoId")]

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            d = self._get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
            out += d.get("items", [])
        return out

    def channels(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(ids), 50):
            d = self._get("channels", part="statistics,snippet", id=",".join(ids[i:i + 50]))
            for it in d.get("items", []):
                out[it["id"]] = it
        return out

    def top_comments(self, video_id: str, n: int = 20) -> list[str]:
        try:
            d = self._get("commentThreads", part="snippet", videoId=video_id, maxResults=n, order="relevance",
                          textFormat="plainText")
        except BlockedError:
            return []  # коментарі вимкнено на відео — не помилка дослідження
        return [it["snippet"]["topLevelComment"]["snippet"].get("textDisplay", "") for it in d.get("items", [])]


def parse_iso_duration(s: str) -> int:
    """PT1H2M3S → секунди."""
    import re
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se

"""YouTube: завантаження на канал власниці (OAuth 2.0 для встановлених застосунків) — лише стандартна бібліотека.

Одноразове налаштування (власниця): Google Cloud → той самий проєкт, що й YOUTUBE_API_KEY → OAuth consent screen
(External, Publishing status «In production» — інакше refresh-токен живе 7 днів) → Credentials → OAuth client ID
(тип **Desktop app**) → завантажити JSON у `data/youtube/client_secret.json` (тека data/ не потрапляє в git);
якщо в JSON немає client_secret (нові клієнти Google показують його лише раз) — `YOUTUBE_CLIENT_SECRET=…` у .env.
Потім `python -m studio_os youtube-auth`: відкривається браузер, власниця входить і натискає «Дозволити»
(для неперевіреного застосунку — «Додатково → Перейти»). Refresh-токен зберігається в `data/youtube/token.json`
разом із каналом, який власниця обрала; агент публікації перевіряє, що вантажить саме на цей канал.

Завантаження: resumable upload (продовжується з місця обриву; URI сесії зберігає агент), `status.privacyStatus=private`
+ `status.publishAt` — відео саме стане публічним у час слоту. Обмеження «неперевірені проєкти → лише private»
Google скасував 2026-11-02 (developers.google.com/youtube/v3/revision_history).
Квота: videos.insert ≈ 100 од., thumbnails.set 50, captions.insert 400, channels/videos.list 1.
Значення токенів і секретів ніколи не логуються.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

from ..errors import BlockedError, RetryableError

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl",        # субтитри, перевірка відео/каналу
          "https://www.googleapis.com/auth/yt-analytics.readonly"]   # Етап 7: аналітика
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3/"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/"
CHUNK = 8 * 1024 * 1024
COST = {"insert": 100, "thumbnail": 50, "caption": 400, "list": 1}

# http(method, url, headers, body) -> (status, headers{lower: value}, body bytes); тести підставляють свій
Http = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # 308 resumable upload — це відповідь, а не переадресація
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=300) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RetryableError(f"YouTube недоступний: {e}") from e


def _reason(body: bytes) -> str:
    try:
        err = json.loads(body.decode("utf-8"))["error"]
        return (err.get("errors") or [{}])[0].get("reason") or err.get("status") or str(err.get("message", ""))[:120]
    except (ValueError, KeyError, TypeError, AttributeError):
        try:
            return json.loads(body.decode("utf-8")).get("error", "")   # формат OAuth: {"error": "invalid_grant"}
        except (ValueError, AttributeError):
            return body[:120].decode("utf-8", "replace")


def _raise_for(status: int, body: bytes, what: str) -> None:
    reason = _reason(body)
    if status == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "uploadLimitExceeded"):
        raise RetryableError(f"YouTube {what}: ліміт ({reason}) — повтор пізніше")
    if status >= 500 or status == 429:
        raise RetryableError(f"YouTube {what}: HTTP {status} {reason}")
    raise BlockedError(f"YouTube {what}: HTTP {status} {reason}")


class YouTubeAuth:
    def __init__(self, folder: Path, http: Http | None = None):
        self.dir, self.http = folder, http or _http
        self.client_path, self.token_path = folder / "client_secret.json", folder / "token.json"
        self._access: tuple[str, float] | None = None

    def client(self) -> dict:
        if not self.client_path.exists():
            raise BlockedError(f"немає OAuth-клієнта: покладіть JSON (тип Desktop app) у {self.client_path.as_posix()} "
                               "і виконайте `python -m studio_os youtube-auth` (docs/studio-os/05-publish.md)")
        raw = json.loads(self.client_path.read_text(encoding="utf-8"))
        c = dict(raw.get("installed") or raw.get("web") or raw)
        if not c.get("client_id"):
            raise BlockedError("client_secret.json не схожий на OAuth-клієнт Google (немає client_id)")
        if not c.get("client_secret"):
            # нові клієнти Google показують секрет лише раз при створенні — у завантаженому JSON його немає
            from ..core import env
            c["client_secret"] = env.get("YOUTUBE_CLIENT_SECRET", self.dir.parents[1] / ".env")
        if not c["client_secret"]:
            raise BlockedError("немає секрету OAuth-клієнта: Google Cloud → Clients → studio-os → Add secret, "
                               "скопіюйте його в .env рядком YOUTUBE_CLIENT_SECRET=… (не в чат)")
        return c

    def token(self) -> dict:
        if not self.token_path.exists():
            raise BlockedError("канал YouTube не підключено: виконайте `python -m studio_os youtube-auth`")
        return json.loads(self.token_path.read_text(encoding="utf-8"))

    def _token_request(self, fields: dict) -> dict:
        st, _, body = self.http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                urllib.parse.urlencode(fields).encode())
        if st != 200:
            if _reason(body) in ("invalid_grant", "unauthorized_client", "invalid_client"):
                raise BlockedError(f"доступ до YouTube відкликано або прострочено ({_reason(body)}) — "
                                   "повторіть `python -m studio_os youtube-auth`")
            _raise_for(st, body, "OAuth")
        return json.loads(body.decode("utf-8"))

    def authorize(self, open_browser: Callable[[str], object] = webbrowser.open, timeout_s: int = 600) -> dict:
        """Вхід власниці в браузері (loopback + PKCE). Повертає збережений токен (без виводу секретів)."""
        c = self.client()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        got: dict = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" in q or "error" in q:
                    got.update({k: v[0] for k, v in q.items()})
                ok = "code" in q and q.get("state", [""])[0] == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(("<h2>Готово — можна закрити вкладку.</h2>" if ok else
                                  "<h2>Вхід не завершено — поверніться в термінал.</h2>").encode())

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        srv.timeout = 5
        redirect = f"http://127.0.0.1:{srv.server_address[1]}"
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        print("Відкриваю браузер для входу в YouTube. Якщо не відкрився — відкрийте посилання вручну:\n" + url, flush=True)
        open_browser(url)
        deadline = time.monotonic() + timeout_s
        try:
            while not got and time.monotonic() < deadline:
                srv.handle_request()
        finally:
            srv.server_close()
        if got.get("state") != state or "code" not in got:
            raise BlockedError(f"вхід не завершено: {got.get('error') or 'немає відповіді (час вийшов)'}")
        tok = self._token_request({"code": got["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
                                   "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
        if not tok.get("refresh_token"):
            raise BlockedError("Google не видав refresh-токен — відкличте доступ застосунку в акаунті й повторіть youtube-auth")
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
        saved = {"refresh_token": tok["refresh_token"], "scope": tok.get("scope", ""), "obtained": int(time.time())}
        self.dir.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(saved), encoding="utf-8")
        return saved

    def save_channel(self, channel: dict) -> None:
        t = self.token()
        t["channel"] = {"id": channel["id"], "title": channel["title"]}
        self.token_path.write_text(json.dumps(t), encoding="utf-8")

    def access_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        c, t = self.client(), self.token()
        tok = self._token_request({"client_id": c["client_id"], "client_secret": c["client_secret"],
                                   "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
        self._access = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
        return self._access[0]


class YouTubeUploader:
    def __init__(self, auth: YouTubeAuth, http: Http | None = None):
        self.auth, self.http = auth, http or auth.http

    def _call(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
              what: str = "API", ok=(200, 201)) -> tuple[int, dict, bytes]:
        for attempt in (1, 2):
            h = {"Authorization": f"Bearer {self.auth.access_token()}", **(headers or {})}
            st, hd, b = self.http(method, url, h, body)
            if st == 401 and attempt == 1:
                self.auth._access = None          # токен доступу прострочився — оновити один раз
                continue
            if st in ok:
                return st, hd, b
            _raise_for(st, b, what)
        raise BlockedError(f"YouTube {what}: 401 після оновлення токена — повторіть youtube-auth")

    def _json(self, method: str, url: str, payload: dict | None = None, what: str = "API") -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        _, _, b = self._call(method, url, {"Content-Type": "application/json; charset=UTF-8"} if body else {}, body, what)
        return json.loads(b.decode("utf-8")) if b else {}

    def my_channel(self) -> dict:
        items = self._json("GET", API + "channels?part=snippet&mine=true", what="channels.list").get("items") or []
        if not items:
            raise BlockedError("в акаунті немає каналу YouTube (channels.list mine=true порожній)")
        return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}

    def start_upload(self, meta: dict, size: int, mime: str = "video/mp4") -> str:
        url = UPLOAD + "videos?uploadType=resumable&part=snippet,status"
        _, hd, _ = self._call("POST", url, {"Content-Type": "application/json; charset=UTF-8",
                                            "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime},
                              json.dumps(meta).encode(), what="videos.insert")
        if not hd.get("location"):
            raise RetryableError("videos.insert: немає URI сесії завантаження")
        return hd["location"]

    def _offset(self, session: str, size: int) -> int | dict:
        """Скільки байтів сервер уже має (308 + Range) або готовий ресурс відео (200/201)."""
        st, hd, b = self.http("PUT", session, {"Authorization": f"Bearer {self.auth.access_token()}",
                                               "Content-Range": f"bytes */{size}", "Content-Length": "0"}, b"")
        if st in (200, 201):
            return json.loads(b.decode("utf-8"))
        if st == 308:
            rng = hd.get("range")
            return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
        if st == 404 or st == 410:
            raise BlockedError("сесія завантаження застаріла (404/410)")
        _raise_for(st, b, "upload")
        return 0

    def upload(self, session: str, path: Path, progress: Callable[[int, int], None] | None = None) -> dict:
        size = path.stat().st_size
        pos = self._offset(session, size)
        if isinstance(pos, dict):
            return pos
        with path.open("rb") as f:
            while True:
                f.seek(pos)
                chunk = f.read(CHUNK)
                end = pos + len(chunk) - 1
                st, hd, b = self.http("PUT", session, {"Authorization": f"Bearer {self.auth.access_token()}",
                                                       "Content-Length": str(len(chunk)),
                                                       "Content-Range": f"bytes {pos}-{end}/{size}"}, chunk)
                if st in (200, 201):
                    return json.loads(b.decode("utf-8"))
                if st == 308:
                    rng = hd.get("range")
                    pos = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
                    if progress:
                        progress(pos, size)
                    continue
                if st == 401:
                    self.auth._access = None
                    continue
                _raise_for(st, b, "upload")

    def video(self, video_id: str) -> dict | None:
        items = self._json("GET", API + f"videos?part=status,snippet&id={urllib.parse.quote(video_id)}",
                           what="videos.list").get("items") or []
        return items[0] if items else None

    def set_thumbnail(self, video_id: str, image: Path) -> None:
        mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
        self._call("POST", UPLOAD + f"thumbnails/set?videoId={urllib.parse.quote(video_id)}&uploadType=media",
                   {"Content-Type": mime}, image.read_bytes(), what="thumbnails.set")

    def insert_caption(self, video_id: str, srt: Path, lang: str, name: str = "") -> None:
        boundary = "studio" + secrets.token_hex(8)
        meta = json.dumps({"snippet": {"videoId": video_id, "language": lang, "name": name, "isDraft": False}})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + srt.read_bytes() \
            + f"\r\n--{boundary}--\r\n".encode()
        self._call("POST", UPLOAD + "captions?uploadType=multipart&part=snippet",
                   {"Content-Type": f"multipart/related; boundary={boundary}"}, body, what="captions.insert")

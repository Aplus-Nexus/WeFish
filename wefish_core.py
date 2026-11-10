"""Small, testable WeRead client core used by the WeFish desktop shell.

The two upstream authentication surfaces stay separate: Gateway requests use
an API key, while Web Reader requests use a cookie jar.  No credential is ever
included in logs or persisted by this module unless the caller supplies a
storage path explicitly.
"""
from __future__ import annotations

import base64
import hashlib
import html as html_lib
import io
import json
import mimetypes
import re
import os
import posixpath
import secrets
import tarfile
import time
import random
import zipfile
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, unquote, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


class WeFishError(RuntimeError):
    pass


class AuthError(WeFishError):
    pass


def apply_environment_credentials(client: "WeReadClient", environ=None) -> bool:
    """Apply optional process credentials without persisting or logging them."""
    values = environ if environ is not None else os.environ
    api_key = str(values.get("WEREAD_API_KEY") or "").strip()
    cookie = str(values.get("WEREAD_COOKIE") or "").strip()
    if api_key:
        client.session.api_key = api_key
    if cookie:
        client.set_cookie_string(cookie)
    return bool(api_key or cookie)


@dataclass
class ReaderSession:
    cookies: CookieJar
    api_key: str = ""
    renewed_at: float = 0.0
    device_fingerprint: str = ""
    web_headers: Dict[str, str] = field(default_factory=dict)
    user_info: Dict[str, Any] = field(default_factory=dict)


class CredentialVault:
    """Optional OS-keychain storage for the WeFish session."""
    SERVICE = "wefish"
    ACCOUNT = "session"

    def __init__(self, backend=None):
        if backend is not None:
            self.backend = backend
            return
        try:
            import keyring
            self.backend = keyring
        except Exception:
            self.backend = None

    def load(self) -> Dict[str, Any]:
        if self.backend is None:
            return {}
        try:
            raw = self.backend.get_password(self.SERVICE, self.ACCOUNT)
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, data: Dict[str, Any]) -> bool:
        if self.backend is None:
            return False
        try:
            self.backend.set_password(self.SERVICE, self.ACCOUNT, json.dumps(data, ensure_ascii=False))
            return True
        except Exception:
            return False


class WeReadClient:
    """Gateway/Web Reader client with bounded retries and JSON-safe errors."""

    GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
    WEB = "https://weread.qq.com"

    def __init__(self, base_url: Optional[str] = None, session: Optional[ReaderSession] = None,
                 timeout: float = 12.0, opener=None, vault: Optional[CredentialVault] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.session = session or ReaderSession(CookieJar())
        self.timeout = timeout
        self.gateway_opener = opener or build_opener()
        self.web_opener = build_opener(HTTPCookieProcessor(self.session.cookies))
        if not self.session.device_fingerprint:
            self.session.device_fingerprint = str(secrets.randbits(32))
        self.vault = vault or CredentialVault()
        self._vault_explicit = vault is not None
        self._reset_qr_session()
        self._catalog_state = {}
        self._reader_state = {}

    @staticmethod
    def _set_cookie_jar_value(jar: CookieJar, name: str, value: str):
        jar.set_cookie(Cookie(0, name, str(value), None, False,
            ".weread.qq.com", True, True, "/", True, False, None, True, None, {}, False))

    @staticmethod
    def _cookie_header(jar: CookieJar) -> str:
        values = [f"{cookie.name}={cookie.value}" for cookie in jar if cookie.value]
        return "; ".join(sorted(values))

    def _reset_qr_session(self):
        self.qr_cookies = CookieJar()
        self._set_cookie_jar_value(self.qr_cookies, "wr_fp", self.session.device_fingerprint)
        self.qr_opener = build_opener(HTTPCookieProcessor(self.qr_cookies))

    def restore_credentials(self) -> bool:
        if self.base_url and not self._vault_explicit:
            return False
        data = self.vault.load()
        if not data:
            return False
        fingerprint = str(data.get("device_fingerprint") or "")
        if fingerprint.isdigit() and 0 < int(fingerprint) <= 4294967295:
            self.session.device_fingerprint = fingerprint
            self._reset_qr_session()
        self.session.api_key = str(data.get("api_key") or "")
        stored_user = data.get("user_info")
        if isinstance(stored_user, dict):
            self.session.user_info = {
                str(key): value for key, value in stored_user.items()
                if isinstance(key, str) and isinstance(value, (str, int, float, bool, type(None)))
            }
        stored_headers = data.get("web_headers")
        if isinstance(stored_headers, dict):
            self.session.web_headers = {
                key: str(value) for key, value in stored_headers.items()
                if key in {"x-wr-ticket", "x-wrpa-0"} and value
            }
        for item in data.get("cookies", []):
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                self._set_cookie_jar_value(self.session.cookies, item["name"], item["value"])
        return bool(self.session.api_key or any(self.session.cookies))

    def persist_credentials(self) -> bool:
        if self.base_url and not self._vault_explicit:
            return False
        cookies = []
        for cookie in self.session.cookies:
            if cookie.domain.endswith("weread.qq.com") or cookie.domain == "weread.qq.com":
                cookies.append({"name": cookie.name, "value": cookie.value})
        return self.vault.save({
            "version": 1,
            "device_fingerprint": self.session.device_fingerprint,
            "api_key": self.session.api_key,
            "user_info": dict(self.session.user_info),
            "cookies": cookies,
            "web_headers": dict(self.session.web_headers),
        })

    def _has_web_credentials(self) -> bool:
        values = {cookie.name: cookie.value for cookie in self.session.cookies}
        # Modern accounts may use wr_gid; older accounts use wr_skey.  A lone
        # wr_vid is an identifier, not an authenticated Web Reader session.
        return len(values.get("wr_skey", "")) >= 8 or len(values.get("wr_gid", "")) >= 5

    def _ensure_web_session(self):
        if self.base_url:
            return
        if not self._has_web_credentials():
            raise AuthError("Web Reader Cookie is not configured; please use 微信扫码登录")
        if time.time() - self.session.renewed_at > 240:
            self.renew()

    def verify_web_session(self) -> Dict[str, Any]:
        """Validate the stored Web Reader Cookie without exposing secrets."""
        self._ensure_web_session()
        values = {cookie.name: cookie.value for cookie in self.session.cookies}
        vid = str(self.session.user_info.get("userVid") or values.get("wr_vid") or "")
        skey = str(values.get("wr_skey") or "")
        if not vid or not skey:
            raise AuthError("Web Reader Cookie is incomplete; please scan again")
        headers = {
            "X-Vid": vid,
            "X-Skey": skey,
            "Cookie": self._cookie_header(self.session.cookies),
            "Referer": f"{self.WEB}/r/weread-skills",
        }
        raw = self._request(
            "GET",
            self._url(f"{self.WEB}/api/userInfo?userVid={quote(vid, safe='')}"),
            headers=headers,
            web_session=True,
        )
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WeFishError("WeRead userInfo returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise WeFishError("WeRead userInfo returned an invalid response")
        self.session.user_info = data
        self.persist_credentials()
        return data

    def set_cookie_string(self, value: str):
        parsed = SimpleCookie(); parsed.load(value or "")
        for morsel in parsed.values():
            self._set_cookie_jar_value(self.session.cookies, morsel.key, morsel.value)

    def qr_begin(self) -> str:
        """Start the official WeRead QR flow and return the opaque UID."""
        # Every scan is an account switch candidate. Do not let an old QR jar
        # or old device response cookies bleed into the next login attempt.
        self._reset_qr_session()
        self._request("GET", self._url(f"{self.WEB}/r/weread-skills"),
                      headers={"Accept": "text/html", "Referer": f"{self.WEB}/"},
                      web_session=True, opener_override=self.qr_opener)
        raw = self._request("GET", self._url(f"{self.WEB}/api/auth/getLoginUid"),
                            headers={"Referer": f"{self.WEB}/r/weread-skills"},
                            web_session=True, opener_override=self.qr_opener)
        data = json.loads(raw.decode("utf-8"))
        uid = data.get("uid")
        if not uid: raise AuthError("WeRead did not return a login UID")
        return str(uid)

    def qr_poll(self, uid: str, otp: str = "") -> Dict[str, Any]:
        if not uid: raise AuthError("QR login UID is empty")
        query = urlencode({"uid": uid, "otp": otp})
        try:
            raw = self._request("GET", self._url(f"{self.WEB}/api/auth/getLoginInfo?{query}"),
                                headers={"Referer": f"{self.WEB}/r/weread-skills"}, retries=0,
                                web_session=True, opener_override=self.qr_opener)
        except AuthError as exc:
            # WeRead has intermittently returned HTTP 401/-2013 from the QR
            # long-poll before the phone confirmation, especially when
            # /r/weread-skills did not issue a bootstrap cookie.  This is a
            # pending QR session, not proof that the user's account failed.
            message = str(exc)
            if "HTTP 401" in message and any(
                marker in message for marker in ("Cgi Request Error", "鉴权失败", "-2013")
            ):
                return {"transport_pending": True, "poll_auth_pending": True}
            raise
        except WeFishError as exc:
            # getLoginInfo is a long-poll endpoint. A transport timeout means
            # “still waiting for the phone”, not a failed login.
            if "network error" in str(exc): return {"transport_pending": True}
            raise
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}

    def qr_complete(self, login: Dict[str, Any]) -> Dict[str, Any]:
        """Exchange a successful QR result for user info and the official key."""
        vid = str(login.get("webLoginVid") or "")
        skey = str(login.get("accessToken") or "")
        if not login.get("succeed") or not vid or not skey:
            raise AuthError("WeRead QR login has not completed")
        # Keep the cookies received while creating/polling the QR session
        # (for example ptcz/RK and any server-issued wr_* values), then
        # replace the previous account's credentials atomically.
        self.session.cookies.clear()
        self.session.web_headers.clear()
        for cookie in self.qr_cookies:
            if cookie.value:
                self._set_cookie_jar_value(self.session.cookies, cookie.name, cookie.value)
        self._set_cookie_jar_value(self.session.cookies, "wr_fp", self.session.device_fingerprint)
        # Cookie values must remain byte-for-byte equal to the login response.
        # URL-encoding the access token here changes the credential itself.
        self._set_cookie_jar_value(self.session.cookies, "wr_vid", vid)
        self._set_cookie_jar_value(self.session.cookies, "wr_skey", skey)
        self._set_cookie_jar_value(self.session.cookies, "wr_ql", "0")
        if login.get("refreshToken"):
            self._set_cookie_jar_value(self.session.cookies, "wr_rt", str(login["refreshToken"]))
        # The Web Reader session is already usable at this point. Persist it
        # before the optional Skill/API-key exchange so a transient key
        # response cannot throw away a successful QR login.
        persisted = self.persist_credentials()
        headers = {
            "X-Vid": vid,
            "X-Skey": skey,
            "Cookie": self._cookie_header(self.session.cookies),
            "Referer": f"{self.WEB}/r/weread-skills",
        }
        try:
            user_raw = self._request("GET", self._url(f"{self.WEB}/api/userInfo?userVid={quote(vid, safe='')}"), headers=headers, web_session=True)
        except AuthError as exc:
            raise AuthError(f"WeRead userInfo authorization failed: {exc}") from exc
        user = json.loads(user_raw.decode("utf-8"))
        self.session.user_info = user if isinstance(user, dict) else {}
        persisted = self.persist_credentials() or persisted
        key = ""
        api = {}
        for attempt in range(3):
            try:
                key_raw = self._request("GET", self._url(f"{self.WEB}/api/skills/apikeyGet?only_show=1"), headers=headers, web_session=True)
                api = json.loads(key_raw.decode("utf-8"))
                key = self._extract_api_key(api)
                break
            except AuthError:
                # Some accounts return HTTP 401 here when WeRead Skill has
                # not been enabled. The QR exchange itself already produced
                # a valid Web Reader Cookie, so keep that session and expose
                # the missing optional key to the UI instead of reporting a
                # failed login.
                if attempt == 2:
                    key = ""
                    break
                time.sleep(0.5)
        if not key:
            # QR authentication and Skill/API-key activation are separate
            # server-side steps. Keep the valid Web Cookie session so the user
            # can enable Skill or paste a key without scanning again.
            persisted = self.persist_credentials() or persisted
            return {"user": user, "api_key": "", "needs_skill": True, "persisted": persisted}
        self.session.api_key = str(key)
        persisted = self.persist_credentials() or persisted
        return {"user": user, "api_key": self.session.api_key, "needs_skill": False, "persisted": persisted}

    @staticmethod
    def _extract_api_key(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        for key in ("apikey", "apiKey", "api_key"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for nested_key in ("data", "result"):
            nested = value.get(nested_key)
            if isinstance(nested, dict):
                found = WeReadClient._extract_api_key(nested)
                if found:
                    return found
        return ""

    def _url(self, canonical: str) -> str:
        if not self.base_url:
            return canonical
        if canonical.startswith(self.GATEWAY):
            return self.base_url + "/api/agent/gateway"
        if canonical.startswith(self.WEB):
            suffix = canonical[len(self.WEB):]
            return self.base_url + suffix
        return canonical

    @staticmethod
    def _public_error(status: int, body: bytes) -> str:
        """Return useful diagnostics without echoing response secrets/content."""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            data = {}
        message = data.get("errmsg") or data.get("message") or data.get("errMsg") or "request failed"
        code = data.get("errcode") or data.get("errCode") or data.get("code")
        suffix = f" ({code})" if code is not None else ""
        return f"HTTP {status}: {str(message)[:180]}{suffix}"

    def _request(self, method: str, url: str, payload: Any = None, headers=None,
                 retries: int = 2, web_session: bool = False, opener_override=None) -> bytes:
        request_headers = {"User-Agent": "WeFish/1.0", "Accept": "application/json"}
        if web_session and opener_override is None:
            request_headers.update(self.session.web_headers)
        request_headers.update(headers or {})
        headers = request_headers
        if isinstance(payload, dict) and headers.get("Content-Type", "").startswith("application/json"):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, dict):
            data = urlencode(payload).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        last = None
        for attempt in range(retries + 1):
            try:
                opener = opener_override or (self.web_opener if web_session else self.gateway_opener)
                response = opener.open(Request(url, data=data, headers=headers, method=method), timeout=self.timeout)
                if web_session:
                    for key in ("x-wr-ticket", "x-wrpa-0"):
                        value = response.headers.get(key)
                        if value:
                            self.session.web_headers[key] = value
                return response.read()
            except HTTPError as exc:
                body = exc.read()
                if exc.code in (401, 403):
                    raise AuthError(self._public_error(exc.code, body)) from exc
                if exc.code < 500 or attempt == retries:
                    raise WeFishError(self._public_error(exc.code, body)) from exc
                last = exc
            except (URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt == retries:
                    raise WeFishError(f"network error after {retries + 1} attempts") from exc
            time.sleep(min(0.25 * (2 ** attempt), 1.0))
        raise WeFishError("request failed") from last

    def gateway(self, api_name: str, **params) -> Dict[str, Any]:
        if not self.session.api_key and not self.base_url:
            raise AuthError("WeRead API key is not configured")
        payload = {"api_name": api_name, "skill_version": "1.0.3", **params}
        headers = {"Content-Type": "application/json"}
        if self.session.api_key:
            headers["Authorization"] = f"Bearer {self.session.api_key}"
        raw = self._request("POST", self._url(self.GATEWAY), payload, headers)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WeFishError("Gateway returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise WeFishError("Gateway returned an invalid response")
        return data

    def web_shelf(self) -> Dict[str, Any]:
        """Read the web shelf with the Web Reader Cookie session."""
        self._ensure_web_session()
        raw = self._request(
            "GET",
            self._url(f"{self.WEB}/web/shelf/sync?{urlencode({'userVid': '', 'synckey': 0})}"),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": self.WEB,
                "Referer": f"{self.WEB}/web/shelf",
            },
            web_session=True,
        )
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WeFishError("Web shelf returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise WeFishError("Web shelf returned an invalid response")
        error_code = data.get("errCode", data.get("errcode", 0))
        if error_code not in (None, "", 0, "0"):
            message = data.get("errMsg") or data.get("errmsg") or "Web Reader Cookie may have expired"
            raise AuthError(f"Web shelf authorization failed: {str(message)[:180]} ({error_code})")
        return data

    def shelf(self):
        # The official Gateway remains preferred when available. A scanned
        # Web Reader session can still populate the shelf without forcing the
        # user to activate the optional Skill API key first.
        if self.session.api_key or (self.base_url and not self._has_web_credentials()):
            return self.gateway("/shelf/sync")
        return self.web_shelf()

    def search(self, keyword: str):
        return self.gateway("/store/search", keyword=keyword, count=20)

    def progress(self, book_id: str):
        return self.gateway("/book/getprogress", bookId=str(book_id))

    def book_info(self, book_id: str):
        return self.gateway("/book/info", bookId=str(book_id))

    def chapter_catalog(self, book_id: str):
        self._ensure_web_session()
        raw = self._request("POST", self._url(f"{self.WEB}/web/book/chapterInfos"),
                            payload={"bookIds": [str(book_id)]},
                            headers={"Content-Type": "application/json;charset=UTF-8", "Origin": self.WEB, "Referer": f"{self.WEB}/web/reader/"}, web_session=True)
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            data = data.get("data") or data.get("books") or [data]
        if not data: raise WeFishError("WeRead returned an empty chapter catalog")
        row = data[0] if isinstance(data, list) else data
        chapters = row.get("updated") or row.get("chapterInfos") or row.get("chapters") or []
        if not hasattr(self, "_catalog_state"): self._catalog_state = {}
        self._catalog_state[str(book_id)] = {
            "synckey": row.get("synckey", ""),
            "copyRightSynckey": row.get("copyRightSynckey", ""),
            "format": row.get("format", ""),
            "chapters": chapters,
        }
        return chapters

    def reader_state(self, book_id: str, chapter_uid: int) -> Dict[str, Any]:
        """Load the authenticated Web Reader bootstrap state.

        The gateway and the Web Reader intentionally have different
        credentials and different protocol state.  In particular, the
        chapter catalog's synckey is not the ``ps`` value accepted by the
        content endpoint; the latter is emitted by the reader HTML as
        ``window.__INITIAL_STATE__.reader.psvts``.
        """
        self._ensure_web_session()
        book_hash = weread_e(book_id)
        chapter_hash = weread_e(chapter_uid)
        reader_url = f"{self.WEB}/web/reader/{book_hash}k{chapter_hash}"
        raw = self._request(
            "GET",
            self._url(reader_url),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": f"{self.WEB}/web/reader/{book_hash}",
            },
            web_session=True,
        )
        state = extract_reader_state(raw.decode("utf-8", "replace"))
        reader = state.get("reader") if isinstance(state, dict) else None
        reader = reader if isinstance(reader, dict) else {}
        psvts = str(reader.get("psvts") or "")
        if not psvts:
            raise WeFishError("Web Reader did not return psvts; the Cookie session may have expired")
        result = {
            "book_id": str((reader.get("bookInfo") or {}).get("bookId") or book_id),
            "title": (reader.get("bookInfo") or {}).get("title", ""),
            "author": (reader.get("bookInfo") or {}).get("author", ""),
            "progress": reader.get("progress") if isinstance(reader.get("progress"), dict) else {},
            "psvts": psvts,
            "pclts": str(reader.get("pclts") or ""),
            "token": str(reader.get("token") or ""),
            "state": state,
        }
        if not hasattr(self, "_reader_state"):
            self._reader_state = {}
        self._reader_state[(str(book_id), int(chapter_uid))] = result
        return result

    def reader_progress(self, book_id: str, chapter_uid: Optional[int] = None) -> Dict[str, Any]:
        """Read a Cookie-only progress snapshot from the Web Reader bootstrap."""
        book_id = str(book_id or "")
        if not book_id:
            raise WeFishError("缺少 bookId")
        if chapter_uid is None:
            shelf = self.web_shelf()
            rows = shelf.get("books", []) if isinstance(shelf, dict) else []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                row_book_id = str(row.get("bookId") or row.get("book_id") or "")
                if row_book_id == book_id:
                    chapter_uid = row.get("chapterUid") or row.get("chapterId") or row.get("currentChapterUid")
                    break
        if chapter_uid is None:
            chapters = self._catalog_state.get(book_id, {}).get("chapters") or self.chapter_catalog(book_id)
            readable = [row for row in chapters if isinstance(row, dict) and int(row.get("wordCount", 1) or 0) > 0]
            chapter_uid = readable[0].get("chapterUid") if readable else None
        try:
            chapter_uid = int(chapter_uid)
        except (TypeError, ValueError) as exc:
            raise WeFishError("Web Reader 没有返回可识别的章节 UID") from exc
        state = self.reader_state(book_id, chapter_uid)
        progress = state.get("progress") if isinstance(state.get("progress"), dict) else {}
        if not progress:
            raise WeFishError("Web Reader 没有返回阅读进度快照")
        result = dict(progress)
        result.setdefault("bookId", book_id)
        result.setdefault("chapterUid", chapter_uid)
        return result

    def import_reader_book(self, reader_url: str) -> Dict[str, Any]:
        """Open a user-supplied Web Reader link using Cookie auth only.

        This is the intentional escape hatch for accounts that have a valid
        Web Reader login but have not enabled the optional Skill API key.  It
        never falls back to the Gateway and only accepts WeRead reader URLs.
        """
        self._ensure_web_session()
        parsed = urlparse(str(reader_url or "").strip())
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
            "weread.qq.com", "www.weread.qq.com"
        } or not (parsed.path.startswith("/web/reader/") or parsed.path.startswith("/web/mp/reader/")):
            raise WeFishError("请粘贴微信读书的 /web/reader/ 或 /web/mp/reader/ 阅读链接")
        is_mp = parsed.path.startswith("/web/mp/reader/")
        canonical = f"{self.WEB}{parsed.path}"
        if parsed.query:
            canonical += f"?{parsed.query}"
        raw = self._request(
            "GET",
            self._url(canonical),
            headers={"Accept": "text/html,application/xhtml+xml", "Referer": f"{self.WEB}/"},
            web_session=True,
        )
        state = extract_reader_state(raw.decode("utf-8", "replace"))
        reader = state.get("reader") if isinstance(state, dict) else None
        reader = reader if isinstance(reader, dict) else {}
        info = reader.get("bookInfo") if isinstance(reader.get("bookInfo"), dict) else {}
        book_id = str(info.get("bookId") or "")
        if not book_id:
            raise WeFishError("阅读链接没有返回书籍信息；请确认已在微信读书登录")
        progress_data = reader.get("progress") if isinstance(reader.get("progress"), dict) else {}
        chapter_offset = progress_data.get(
            "chapterOffset",
            progress_data.get("chapterPos", progress_data.get("offset", reader.get("chapterOffset", 0))),
        )

        if is_mp:
            articles = self.mp_articles(book_id)
            if not articles:
                raise WeFishError("公众号阅读链接没有返回可读文章")
            current = reader.get("currentArticle") if isinstance(reader.get("currentArticle"), dict) else {}
            current_id = str(current.get("reviewId") or "")
            if not current_id or not any(str(row.get("reviewId")) == current_id for row in articles):
                current_id = str(articles[0].get("reviewId") or "")
            return {
                "bookId": book_id,
                "title": info.get("title") or book_id,
                "author": info.get("author", "公众号"),
                "progress": progress_data.get("progress", 0),
                "chapterOffset": chapter_offset,
                "chapterUid": current_id,
                "readerUrl": canonical,
                "isMp": True,
                "articles": articles,
            }

        chapters = self.chapter_catalog(book_id)
        readable = [
            chapter for chapter in chapters
            if int(chapter.get("wordCount", 1) or 0) > 0 and chapter.get("title") != "封面"
        ]
        current = reader.get("currentChapter") if isinstance(reader.get("currentChapter"), dict) else {}
        current_uid = current.get("chapterUid")
        if current_uid is None and readable:
            current_uid = readable[0].get("chapterUid")
        if current_uid is None:
            raise WeFishError("阅读链接没有可读章节")
        try:
            current_uid = int(current_uid)
        except (TypeError, ValueError) as exc:
            raise WeFishError("阅读链接返回了无法识别的章节 UID") from exc

        psvts = str(reader.get("psvts") or "")
        if psvts:
            self._reader_state[(book_id, current_uid)] = {
                "book_id": book_id,
                "title": info.get("title", ""),
                "author": info.get("author", ""),
                "psvts": psvts,
                "pclts": str(reader.get("pclts") or ""),
                "token": str(reader.get("token") or ""),
                "state": state,
            }
        return {
            "bookId": book_id,
            "title": info.get("title") or book_id,
            "author": info.get("author", ""),
            "progress": progress_data.get("progress", 0),
            "chapterOffset": chapter_offset,
            "chapterUid": current_uid,
            "readerUrl": canonical,
            "chapters": chapters,
        }

    @staticmethod
    def is_mp_book(book_id: Any) -> bool:
        return str(book_id or "").startswith("MP_WXS_")

    @staticmethod
    def entitlement_summary(chapters) -> Dict[str, Any]:
        """Summarize entitlement flags exposed by the catalog.

        WeRead does not expose a separate public membership endpoint in the
        skill contract.  The catalog does expose per-chapter ``paid`` and
        ``price`` fields, so the desktop UI can show what the account can
        see without guessing or bypassing server-side entitlement checks.
        """
        paid_rows = []
        prices = []
        for row in chapters if isinstance(chapters, list) else []:
            if not isinstance(row, dict):
                continue
            paid = row.get("paid") in {True, 1, "1", "true", "True"}
            try:
                price = float(row.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if paid or price > 0:
                paid_rows.append(row)
                if price > 0:
                    prices.append(price)
        return {
            "has_paid_content": bool(paid_rows),
            "paid_chapters": len(paid_rows),
            "prices": prices,
        }

    @staticmethod
    def membership_summary(reader_state: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize the membership snapshot embedded in Web Reader state.

        The documented Gateway contract does not expose a standalone member
        endpoint.  When the authenticated Web Reader bootstrap includes
        ``reader.memberCardSummary``, keep only safe membership fields for the
        desktop UI.  An empty summary is deliberately reported as unknown;
        it must not be interpreted as an expired membership.
        """
        root = reader_state if isinstance(reader_state, dict) else {}
        reader = root.get("reader") if isinstance(root.get("reader"), dict) else {}
        raw = reader.get("memberCardSummary") if isinstance(reader.get("memberCardSummary"), dict) else {}

        def first(*names):
            for name in names:
                value = raw.get(name)
                if value not in (None, ""):
                    return value
            return None

        def boolean(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "active", "valid"}
            return None

        remaining = first("remainTime", "remainingTime", "remainingSeconds", "remainSeconds")
        try:
            remaining = max(0, int(float(remaining))) if remaining is not None else None
        except (TypeError, ValueError):
            remaining = None
        expires = first("expiredTime", "expireTime", "endTime", "memberEndTime", "mcardEndTime")
        explicit = boolean(first("isMember", "isVip", "member", "valid", "enabled"))
        status = "unknown"
        if explicit is not None:
            status = "active" if explicit else "inactive"
        elif remaining is not None:
            status = "active" if remaining > 0 else "inactive"
        elif expires is not None:
            try:
                epoch = float(expires)
                if epoch > 100000000000:
                    epoch /= 1000
                status = "active" if epoch > time.time() else "inactive"
            except (TypeError, ValueError):
                pass

        return {
            "status": status,
            "source": "web-reader-bootstrap",
            "expiresAt": expires,
            "remainingSeconds": remaining,
            "cardType": first("cardType", "memberType", "type", "name"),
            "hasSnapshot": bool(raw),
        }

    def reader_membership(self, book_id: str, chapter_uid: Optional[int] = None) -> Dict[str, Any]:
        """Read account membership hints without exposing reader credentials."""
        book_id = str(book_id or "")
        if chapter_uid is None:
            chapters = self._catalog_state.get(book_id, {}).get("chapters") or self.chapter_catalog(book_id)
            readable = [row for row in chapters if isinstance(row, dict) and int(row.get("wordCount", 1) or 0) > 0]
            if not readable:
                raise WeFishError("没有可用于读取会员状态的章节")
            chapter_uid = readable[0].get("chapterUid")
        state = self.reader_state(book_id, int(chapter_uid))
        return self.membership_summary(state.get("state", {}))

    def mp_articles(self, book_id: str, offset: int = 0):
        """Load and normalize public-account articles for an MP shelf item."""
        book_id = str(book_id or "")
        if not self.is_mp_book(book_id):
            raise WeFishError("不是公众号书籍")
        self._ensure_web_session()
        query = urlencode({"bookId": book_id, "offset": max(0, int(offset))})
        raw = self._request(
            "GET",
            self._url(f"{self.WEB}/web/mp/articles?{query}"),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{self.WEB}/web/mp/reader/{weread_e(book_id)}",
            },
            web_session=True,
        )
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WeFishError("公众号文章接口返回了无效 JSON") from exc
        articles = []
        for group in data.get("reviews", []) if isinstance(data, dict) else []:
            if not isinstance(group, dict):
                continue
            for sub in group.get("subReviews", []) if isinstance(group.get("subReviews"), list) else []:
                if not isinstance(sub, dict):
                    continue
                review = sub.get("review") if isinstance(sub.get("review"), dict) else sub
                mp_info = review.get("mpInfo") if isinstance(review.get("mpInfo"), dict) else {}
                review_ids = []
                for value in (sub.get("reviewId"), review.get("reviewId"), mp_info.get("originalId")):
                    value = str(value or "")
                    if value and value not in review_ids:
                        review_ids.append(value)
                review_id = str(review.get("reviewId") or sub.get("reviewId") or "")
                if not review_id:
                    continue
                articles.append({
                    "reviewId": review_id,
                    "reviewIds": review_ids,
                    "originalId": str(mp_info.get("originalId") or ""),
                    "bookId": str(review.get("belongBookId") or book_id),
                    "sourceUrl": str(
                        mp_info.get("content_url") or mp_info.get("contentUrl")
                        or mp_info.get("source_url") or mp_info.get("sourceUrl")
                        or mp_info.get("url") or review.get("contentUrl")
                        or review.get("sourceUrl") or review.get("url") or ""
                    ),
                    "title": str(mp_info.get("title") or "未命名文章"),
                    "picUrl": str(mp_info.get("pic_url") or ""),
                    "createTime": review.get("createTime", 0),
                })
        return articles

    def mp_content(self, review_id: str, *, inline_images: bool = True) -> str:
        """Fetch one public-account article body using Web Reader cookies."""
        review_id = str(review_id or "")
        if not review_id:
            raise WeFishError("公众号文章 ID 为空")
        self._ensure_web_session()
        raw = self._request(
            "GET",
            self._url(f"{self.WEB}/web/mp/content?{urlencode({'reviewId': review_id})}"),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": f"{self.WEB}/web/mp/reader/",
            },
            web_session=True,
        )
        body = extract_mp_body(raw.decode("utf-8", "replace"))
        if not body:
            raise WeFishError("公众号文章没有返回可读正文")
        return self._inline_mp_images(body) if inline_images else body

    def _inline_mp_images(self, source: str) -> str:
        """Inline whitelisted public-account images for QTextBrowser/offline cache."""
        replacements = {}
        for match in re.finditer(r"(\bsrc\s*=\s*)([\"'])(.*?)(?:\2)", source, flags=re.I | re.S):
            original = html_lib.unescape(match.group(3)).strip()
            if original.startswith("//"):
                original = "https:" + original
            parsed = urlparse(original)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            host = parsed.hostname.lower()
            if not (host == "mmbiz.qpic.cn" or host.endswith(".mmbiz.qpic.cn") or host == "mmbiz.qlogo.cn" or host.endswith(".mmbiz.qlogo.cn")):
                continue
            if original in replacements:
                continue
            try:
                data = self._request(
                    "GET", original,
                    headers={"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "Referer": f"{self.WEB}/"},
                    # Public MP images do not need WeRead cookies. Keep the
                    # Web Reader session headers off third-party image hosts.
                    web_session=False,
                )
                if not data or len(data) > 8 * 1024 * 1024:
                    continue
                media = mimetypes.guess_type(parsed.path)[0] or "image/jpeg"
                replacements[original] = f"data:{media};base64,{base64.b64encode(data).decode('ascii')}"
            except WeFishError:
                continue
        for original, replacement in replacements.items():
            source = source.replace(original, replacement)
        return source

    def fetch_chapter(self, book_id: str, chapter_uid: int):
        book_id = str(book_id); chapter_uid = int(chapter_uid)
        # Fetching reader HTML is deliberate: catalog synckey/copyRightSynckey
        # identifies a catalog revision, but cannot authorize full content.
        catalog = self._catalog_state.get(book_id, {}).get("chapters") or self.chapter_catalog(book_id)
        state = self.reader_state(book_id, chapter_uid)
        psvts = state["psvts"]
        params = content_params(book_id, chapter_uid, psvts)
        source = self.read_chapter(
            weread_e(book_id),
            weread_e(chapter_uid),
            params=params,
            book_id=book_id,
            chapter_uid=chapter_uid,
        )
        chapter = next((row for row in catalog if str(row.get("chapterUid")) == str(chapter_uid)), {})
        return self._embed_chapter_assets(source, chapter)

    def _embed_chapter_assets(self, source: str, chapter: Dict[str, Any]) -> str:
        """Inline chapter images from the catalog's tar resource package."""
        if not source or "<img" not in source.lower():
            return source
        tar_url = str(chapter.get("tar") or "")
        if not tar_url:
            return source
        try:
            # The catalog has returned absolute, protocol-relative, and
            # root-relative resource URLs across reader deployments. Resolve
            # same-host paths through the test/proxy base URL while leaving
            # res.weread.qq.com assets on their original host.
            canonical = urljoin(f"{self.WEB}/", tar_url)
            raw = self._request(
                "GET",
                self._url(canonical) if urlparse(canonical).netloc == urlparse(self.WEB).netloc else canonical,
                headers={"Accept": "application/x-tar, application/octet-stream", "Referer": f"{self.WEB}/web/reader/"},
                web_session=True,
            )
            assets = {}
            try:
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
                    for member in archive.getmembers():
                        if not member.isfile():
                            continue
                        handle = archive.extractfile(member)
                        if handle is None:
                            continue
                        data = handle.read()
                        member_name = member.name
                        name = posix_basename(member_name)
                        if name:
                            assets[name] = _image_data_uri(name, data)
            except tarfile.ReadError:
                # Cloud-converted books have been observed to use a ZIP while
                # retaining the catalog field name `tar`.
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        name = posix_basename(member.filename)
                        if name:
                            assets[name] = _image_data_uri(name, archive.read(member))
            if not assets:
                return source

            def replace_image(match):
                prefix, quote_mark, src = match.groups()
                name = posix_basename(unquote(urlparse(src).path))
                return f"{prefix}{quote_mark}{assets.get(name, src)}{quote_mark}"

            return re.sub(
                r"(<img\b[^>]*\bsrc\s*=\s*)([\"'])(.*?)(?:\2)",
                replace_image,
                source,
                flags=re.I | re.S,
            )
        except (OSError, tarfile.TarError, WeFishError):
            # A missing optional asset package must not make readable text
            # unavailable; keep the decoded XHTML and continue.
            return source

    def reviews(self, book_id: str):
        return self.gateway("/review/list", bookId=str(book_id))

    def bookmarks(self, book_id: str):
        """Return the account's actual highlighted text for one book."""
        return self.gateway("/book/bookmarklist", bookId=str(book_id))

    def my_reviews(self, book_id: str, count: int = 100, synckey: int = 0):
        """Return the account's own reading thoughts for one book."""
        return self.gateway(
            "/review/list/mine",
            bookid=str(book_id),
            synckey=int(synckey),
            count=int(count),
        )

    @staticmethod
    def _normalize_review_rows(rows):
        normalized = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            review = row.get("review") if isinstance(row.get("review"), dict) else row
            if isinstance(review.get("review"), dict):
                review = review["review"]
            content = review.get("content") or review.get("htmlContent") or review.get("abstract") or ""
            if not content:
                continue
            normalized.append({
                "content": str(content),
                "reviewId": str(review.get("reviewId") or row.get("reviewId") or ""),
                "chapterUid": review.get("chapterUid", row.get("chapterUid")),
                "chapterName": str(review.get("chapterName") or row.get("chapterName") or ""),
                "createTime": review.get("createTime", row.get("createTime", 0)),
            })
        return normalized

    @staticmethod
    def _filter_chapter_rows(rows, chapter_uid):
        rows = [row for row in rows if isinstance(row, dict)]
        tagged = [row for row in rows if row.get("chapterUid") is not None]
        if not tagged:
            return rows
        return [row for row in tagged if str(row.get("chapterUid")) == str(chapter_uid)]

    def _chapter_thoughts(self, book_id: str, chapter_uid: int, ranges):
        if not ranges:
            return []
        request_ranges = [
            {"range": value, "maxIdx": 0, "count": 30, "synckey": 0}
            for value in ranges
        ]
        data = self.gateway(
            "/book/readreviews",
            bookId=str(book_id),
            chapterUid=int(chapter_uid),
            reviews=request_ranges,
        )
        rows = []
        for group in data.get("reviews", []) if isinstance(data, dict) else []:
            if not isinstance(group, dict):
                continue
            page_rows = group.get("pageReviews")
            if isinstance(page_rows, list):
                rows.extend(page_rows)
            elif group.get("content") or group.get("review"):
                rows.append(group)
        return self._normalize_review_rows(rows)

    def read_stats(self, mode="monthly", base_time=None):
        payload = {"mode": mode}
        if base_time:
            payload["baseTime"] = int(base_time)
        return self.gateway("/readdata/detail", **payload)

    def notebooks(self, count=20):
        return self.gateway("/user/notebooks", count=int(count), lastSort=0)

    def report_read(self, payload: Dict[str, Any]):
        """Report a signed Web Reader reading position/session."""
        self._ensure_web_session()
        payload = dict(payload or {})
        book_id = str(payload.get("bookId") or payload.get("book_id") or "")
        chapter_uid = payload.get("chapterUid", payload.get("chapter_uid"))
        if book_id and chapter_uid is not None:
            chapter_uid = int(chapter_uid)
            state = getattr(self, "_reader_state", {}).get((book_id, chapter_uid))
            if not state:
                state = self.reader_state(book_id, chapter_uid)
            payload = read_report_params(
                book_id=book_id,
                chapter_uid=chapter_uid,
                chapter_idx=payload.get("chapterIdx", payload.get("ci", 0)),
                chapter_offset=payload.get("chapterOffset", payload.get("co", 0)),
                progress=payload.get("progress", payload.get("pr", 0)),
                summary=payload.get("summary", payload.get("sm", "")),
                psvts=state.get("psvts", ""),
                pclts=state.get("pclts", ""),
                token=state.get("token", ""),
                elapsed_seconds=payload.get("elapsed", payload.get("rt", 0)),
            )
        raw = self._request("POST", self._url(f"{self.WEB}/web/book/read"), payload=payload,
                            headers={"Content-Type": "application/json;charset=UTF-8", "Origin": self.WEB, "Referer": f"{self.WEB}/web/reader/"}, web_session=True)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw": "ok"}

    def annotations(self, book_id: str, chapter_uid: int):
        bookmark_data = self.bookmarks(book_id)
        rows = bookmark_data.get("updated") or bookmark_data.get("underlines") or []
        underlines = self._filter_chapter_rows(rows, chapter_uid)
        ranges = []
        for row in underlines:
            value = str(row.get("range") or "")
            if value and value not in ranges:
                ranges.append(value)
        thoughts = self._chapter_thoughts(book_id, chapter_uid, ranges)
        mine_data = self.my_reviews(book_id)
        mine = self._filter_chapter_rows(
            self._normalize_review_rows(mine_data.get("reviews", []) if isinstance(mine_data, dict) else []),
            chapter_uid,
        )
        return {"underlines": underlines, "reviews": thoughts, "my_reviews": mine}

    def read_chapter(
        self,
        book_hash: str,
        chapter_hash: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        book_id: Optional[str] = None,
        chapter_uid: Optional[int] = None,
    ) -> str:
        """Proxy the authenticated Web Reader chapter protocol.

        ``book_hash`` and ``chapter_hash`` are kept in the public signature
        for compatibility with the reference plugin.  Real chapter access
        uses JSON POSTs to the e/t shard endpoints, the cookie session, and
        the signed parameters prepared from reader ``psvts``.
        """
        self._ensure_web_session()
        if params is None:
            params = {"b": book_hash, "c": chapter_hash}
        referer = f"{self.WEB}/web/reader/{book_hash}"

        def post_shard(name: str, *, style: bool = False) -> str:
            shard_params = dict(params)
            shard_params["st"] = 1 if style else 0
            # The signature covers every field except s, so changing st for
            # the stylesheet requires a fresh signature.
            if "s" in shard_params:
                shard_params.pop("s")
                shard_params["s"] = weread_sign(shard_params)
            raw = self._request(
                "POST",
                self._url(f"{self.WEB}/web/book/chapter/{name}"),
                payload=shard_params,
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "Origin": self.WEB,
                    "Referer": referer,
                },
                web_session=True,
            )
            return raw.decode("utf-8", "replace")

        e0 = post_shard("e_0")
        try:
            first = json.loads(e0)
        except json.JSONDecodeError:
            first = None
        if isinstance(first, dict) and first.get("bookId"):
            # TXT books answer the EPUB probe with chapter metadata.  Their
            # readable body is split across t_0/t_1.
            t0 = post_shard("t_0")
            t1 = ""
            try:
                t1 = post_shard("t_1")
            except WeFishError:
                pass
            if _is_mock_json_shard(t0):
                return _join_mock_html(t0, t1)
            return decode_content_shards(t0, t1, "")

        e1 = post_shard("e_1")
        e3 = post_shard("e_3")
        if all(_is_mock_json_shard(part) for part in (e0, e1, e3)):
            return _join_mock_html(e0, e1, e3)
        source = decode_content_shards(e0, e1, e3)
        # EPUB books carry their stylesheet in e_2.  It is optional for a
        # readable fallback, so an unavailable style shard must not hide text.
        try:
            e2 = post_shard("e_2", style=True)
            if not _is_mock_json_shard(e2):
                style = decode_content_shard(e2)
                if style:
                    if re.search(r"<head\b", source, flags=re.I):
                        source = re.sub(
                            r"(<head\b[^>]*>)",
                            lambda match: match.group(1) + f"<style>{style}</style>",
                            source,
                            count=1,
                            flags=re.I,
                        )
                    else:
                        source = f"<style>{style}</style>{source}"
        except (WeFishError, UnicodeError):
            pass
        return source

    def renew(self):
        raw = self._request("POST", self._url(f"{self.WEB}/web/login/renewal"),
                            payload=json.dumps({"rq": "%2Fweb%2Fbook%2Fread", "ql": False}),
                            headers={"Content-Type": "application/json;charset=UTF-8", "Origin": self.WEB, "Referer": f"{self.WEB}/"}, web_session=True)
        data = json.loads(raw.decode("utf-8"))
        if data.get("succ") != 1:
            raise AuthError("WeRead session renewal failed")
        self.session.renewed_at = time.time()
        self.persist_credentials()
        return True


class LocalCache:
    """JSON cache with atomic replacement and no credential logging."""
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or (Path.home() / ".wefish" / "cache"))
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, value: Any):
        safe = "".join(ch for ch in str(key) if ch.isalnum() or ch in "-_\u4e00-\u9fff")[:120] or "item"
        target = self.root / f"{safe}.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)

    def get(self, key: str, default=None):
        target = self.root / f"{key}.json"
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default


def _checked_body(shard: str) -> str:
    if not shard:
        return ""
    if len(shard) <= 32:
        raise WeFishError("chapter shard is too short")
    digest, body = shard[:32], shard[32:]
    if hashlib.md5(body.encode("utf-8")).hexdigest().upper() != digest.upper():
        raise WeFishError("chapter shard checksum mismatch")
    return body


def weread_e(value: Any) -> str:
    """Encode reader ids using the scheme used by WeRead's web reader."""
    value = str(value); digest = hashlib.md5(value.encode()).hexdigest(); result = digest[:3]
    if value.isdigit():
        chunks = [format(int(value[i:i + 9]), "x") for i in range(0, len(value), 9)]; flag = "3"
    else:
        chunks = ["".join(format(ord(ch), "x") for ch in value)]; flag = "4"
    result += flag + "2" + digest[-2:]
    for i, chunk in enumerate(chunks): result += format(len(chunk), "x").zfill(2) + chunk + ("g" if i < len(chunks) - 1 else "")
    if len(result) < 20: result += digest[:20 - len(result)]
    return result + hashlib.md5(result.encode()).hexdigest()[:3]


def _js_string(value: Any) -> str:
    if value is True: return "true"
    if value is False: return "false"
    if value is None: return "null"
    return str(value)


def weread_sign(params: Dict[str, Any]) -> str:
    # The web client uses encodeURIComponent, whose space encoding is %20
    # rather than application/x-www-form-urlencoded's '+'.
    query = "&".join(f"{key}={quote(_js_string(params[key]), safe='')}" for key in sorted(params))
    a = b = 0x15051505; i = len(query) - 1
    while i > 0:
        a = (a ^ (ord(query[i]) << ((len(query) - i) % 30))) & 0x7FFFFFFF
        b = (b ^ (ord(query[i - 1]) << (i % 30))) & 0x7FFFFFFF; i -= 2
    return format(a + b, "x").lower()


def content_params(book_id: str, chapter_uid: int, psvts: str, style: bool = False) -> Dict[str, Any]:
    ct_value = int(time.time())
    ct = str(ct_value)
    # WeRead can answer {} when pc == ps.  The browser advances the clock in
    # that rare collision, so mirror that behavior here.
    if weread_e(ct) == str(psvts):
        ct_value += 1
        ct = str(ct_value)
    params: Dict[str, Any] = {"b": weread_e(book_id), "c": weread_e(chapter_uid),
        "r": str(random.randint(0, 9999) ** 2), "ct": ct, "ps": str(psvts or "0"),
        "pc": weread_e(ct), "sc": 1, "prevChapter": False, "st": 1 if style else 0}
    params["s"] = weread_sign(params)
    return params


def read_report_params(*, book_id: str, chapter_uid: int, chapter_idx: int = 0,
                       chapter_offset: int = 0, progress: Any = 0, summary: str = "",
                       psvts: str, pclts: str = "", token: str = "",
                       elapsed_seconds: int = 0) -> Dict[str, Any]:
    now = int(time.time())
    ts = now * 1000 + random.randint(0, 999)
    rn = random.randint(0, 999)
    try:
        progress_value = max(0, min(100, int(float(progress))))
    except (TypeError, ValueError):
        progress_value = 0
    try:
        elapsed_value = max(0, int(elapsed_seconds))
    except (TypeError, ValueError):
        elapsed_value = 0
    pc = str(pclts or "")
    if not pc or pc == "0":
        pc = weread_e(now)
    params: Dict[str, Any] = {
        "b": weread_e(book_id),
        "c": weread_e(chapter_uid or 0),
        "ci": int(chapter_idx or 0),
        "co": max(0, int(chapter_offset or 0)),
        "sm": str(summary or "")[:20],
        "pr": progress_value,
        "rt": elapsed_value,
        "ts": ts,
        "rn": rn,
        "ct": now,
        "ps": str(psvts or ""),
        "pc": pc,
    }
    if token:
        params["sg"] = hashlib.sha256(f"{ts}{rn}{token}".encode()).hexdigest()
    params["s"] = weread_sign(params)
    return params


class _MPBodyExtractor(HTMLParser):
    """Extract js_content/rich_media_content while preserving nested markup."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth = 0
        self.suppressed = 0
        self.parts = []

    def _is_target(self, attrs):
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        classes = set(values.get("class", "").split())
        return values.get("id") == "js_content" or "rich_media_content" in classes

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.depth == 0:
            if self._is_target(attrs):
                self.depth = 1
            return
        if self.suppressed:
            if tag in {"script", "style"}:
                self.suppressed += 1
            return
        if tag in {"script", "style"}:
            self.suppressed = 1
            return
        self.parts.append(self.get_starttag_text() or f"<{tag}>")
        self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.depth and not self.suppressed and tag.lower() not in {"script", "style"}:
            self.parts.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self.depth:
            return
        if self.suppressed:
            if tag in {"script", "style"}:
                self.suppressed -= 1
            return
        self.depth -= 1
        if self.depth:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.depth and not self.suppressed:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.depth and not self.suppressed:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.depth and not self.suppressed:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        if self.depth and not self.suppressed:
            self.parts.append(f"<!--{data}-->")


def extract_mp_body(article_html: str) -> str:
    """Return readable MP article HTML without scripts or page chrome."""
    parser = _MPBodyExtractor()
    try:
        parser.feed(str(article_html or ""))
        parser.close()
    except Exception:
        parser.parts = []
    body = "".join(parser.parts)
    if not body:
        # Keep a tolerant fallback for malformed pages whose closing tags are
        # omitted by the server.  The parser path is preferred for nested divs.
        match = re.search(
            r"<div[^>]*(?:id=['\"]js_content['\"]|class=['\"][^'\"]*rich_media_content[^'\"]*['\"])[^>]*>(.*)",
            str(article_html or ""), flags=re.I | re.S,
        )
        body = match.group(1) if match else ""
    body = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", body, flags=re.I | re.S)
    body = re.sub(r"<style\b[^>]*>.*?</style\s*>", "", body, flags=re.I | re.S)
    body = re.sub(r"\s+(?:data-)?src\s*=\s*(['\"])\1", "", body, flags=re.I)
    body = re.sub(r"\s+data-src\s*=", " src=", body, flags=re.I)
    return body.strip()


def extract_reader_state(reader_html: str) -> Dict[str, Any]:
    """Extract the JSON bootstrap object emitted by the Web Reader page."""
    # The surrounding page script has changed several times. Decode the JSON
    # object by its own boundary instead of requiring a particular JavaScript
    # function to follow it.
    anchor = re.search(r"window\.__INITIAL_STATE__\s*=\s*", reader_html)
    if anchor:
        candidate = reader_html[anchor.end():].lstrip()
        try:
            data, _end = json.JSONDecoder().raw_decode(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # Keep the older exact form as a tolerant fallback for malformed HTML
    # where the JSON decoder cannot find a clean object boundary.
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*\(function",
        reader_html,
        flags=re.S,
    )
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # Some edge deployments serialize the bootstrap object differently. The
    # reader values below are enough for the content bridge and avoid logging
    # or persisting the full HTML page.
    def value(name: str) -> str:
        found = re.search(rf'"{re.escape(name)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', reader_html)
        return json.loads(f'"{found.group(1)}"') if found else ""

    return {"reader": {
        "bookInfo": {"bookId": value("bookId"), "title": value("title"), "author": value("author")},
        "psvts": value("psvts"),
        "pclts": value("pclts"),
        "token": value("token"),
    }}


def posix_basename(value: str) -> str:
    return posixpath.basename(str(value or "").split("?", 1)[0].split("#", 1)[0])


def _image_data_uri(name: str, data: bytes) -> str:
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if data.startswith(b"\x89PNG"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _is_mock_json_shard(value: str) -> bool:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and "html" in data


def _join_mock_html(*parts: str) -> str:
    rendered = []
    for part in parts:
        try:
            data = json.loads(part)
        except (TypeError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and "html" in data:
            rendered.append(str(data["html"]))
    return "".join(rendered)


def _swap_positions(encoded: str):
    length = len(encoded)
    if length < 4: return []
    if length < 11: return [0, 2]
    n = min(4, (length + 9) // 10); tmp = ""
    for i in range(length - 1, length - n - 1, -1): tmp += str(int(bin(ord(encoded[i]))[2:], 4))
    result = []; m = length - n - 2; step = len(str(m)); i = 0
    while len(result) < 10 and i + step < len(tmp):
        result.extend((int(tmp[i:i + step]) % m, int(tmp[i + 1:i + 1 + step]) % m)); i += step
    return result


def decode_content_shards(e0: str, e1: str, e3: str) -> str:
    payload = _checked_body(e0) + _checked_body(e1) + _checked_body(e3)
    chars = list(payload[1:]); positions = _swap_positions(payload[1:])
    for i in range(len(positions) - 1, -1, -2):
        for k in (1, 0):
            left, right = positions[i] + k, positions[i - 1] + k
            chars[left], chars[right] = chars[right], chars[left]
    encoded = re.sub(r"[^A-Za-z0-9+/]", "", "".join(chars).replace("-", "+").replace("_", "/"))
    binary = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("latin-1")
    out = []; i = 0
    while i < len(binary):
        b = [ord(x) for x in binary[i:i + 4]]
        size = 1
        if 0xC0 <= b[0] <= 0xDF: size = 2
        elif 0xE0 <= b[0] <= 0xEF: size = 3
        elif 0xF0 <= b[0] <= 0xF7: size = 4
        if size == 1:
            out.append(binary[i]); i += 1; continue
        if size > len(b) or any(not 0x80 <= b[j] <= 0xBF for j in range(1, size)):
            out.append(binary[i]); i += 1; continue
        code = b[0] & (0x7F >> size)
        for j in range(1, size): code = (code << 6) | (b[j] & 0x3F)
        out.append(chr(code)); i += size
    return "".join(out)


def decode_content_shard(shard: str) -> str:
    """Decode one independently encoded Web Reader shard such as e_2."""
    return decode_content_shards(shard, "", "")


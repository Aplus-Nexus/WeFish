"""Small localhost bridge for the Electron WeFish shell.

The browser UI never receives API keys, cookies, or reader session headers.
Those stay inside the Python process that owns WeReadClient and its OS vault.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from wefish_core import AuthError, WeReadClient, WeFishError, apply_environment_credentials

# The bundled macOS keyring backend can take several seconds on its first
# access while the login keychain wakes up. Treat that delay as loading rather
# than turning an already persisted session into a false login failure.
CREDENTIAL_RESTORE_TIMEOUT = 20.0


def _safe_state(client: WeReadClient):
    cookies = {cookie.name: cookie.value for cookie in client.session.cookies}
    has_web = bool(len(cookies.get("wr_skey", "")) >= 8 or len(cookies.get("wr_gid", "")) >= 5)
    return {
        "hasWebCookie": has_web,
        "hasGatewayKey": bool(client.session.api_key),
        "accountName": str(client.session.user_info.get("name") or client.session.user_info.get("nickName") or ""),
    }


class Bridge(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, client):
        super().__init__(address, Handler)
        self.client = client
        self.lock = threading.RLock()
        self.qr_uid = ""
        self.qr_image = ""
        self.mock_server = None
        self.credentials_loading = False
        self.credentials_error = False
        self.credentials_cancelled = False


def _restore_credentials_with_timeout(client: WeReadClient, timeout: float = CREDENTIAL_RESTORE_TIMEOUT):
    """Restore OS credentials without allowing a locked vault to stall the bridge."""
    result = {}
    done = threading.Event()

    def restore():
        try:
            restored = WeReadClient(timeout=client.timeout, vault=client.vault)
            restored.restore_credentials()
            result["client"] = restored
        except Exception as exc:  # noqa: BLE001 - service reports a safe boolean
            result["error"] = exc
        finally:
            done.set()

    threading.Thread(target=restore, name="wefish-credential-store", daemon=True).start()
    if not done.wait(max(0.001, float(timeout))):
        return None, "timeout"
    if result.get("error") is not None:
        return None, "error"
    return result.get("client"), "ok"


def _remote_progress(client: WeReadClient, book_id: str):
    """Read cloud position from Gateway, with the cookie shelf as fallback."""
    try:
        return client.progress(book_id), "weread-gateway"
    except AuthError:
        shelf = client.web_shelf()
        rows = shelf.get("books", []) if isinstance(shelf, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("bookId") or row.get("book_id")) == book_id:
                chapter_uid = row.get("chapterUid") or row.get("chapterId") or row.get("currentChapterUid")
                if chapter_uid is not None:
                    try:
                        return {"book": client.reader_progress(book_id, chapter_uid)}, "weread-web-reader"
                    except (AuthError, WeFishError, ValueError):
                        pass
                return {"book": row}, "weread-web-shelf"
        raise


class Handler(BaseHTTPRequestHandler):
    server_version = "WeFishBridge/1"

    def log_message(self, *_):
        return

    @property
    def bridge(self) -> Bridge:
        return self.server  # type: ignore[return-value]

    def _headers(self, content_type="application/json"):
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self._headers(); self.end_headers(); self.wfile.write(raw)

    def _html(self, status, source):
        raw = str(source).encode("utf-8")
        self.send_response(status); self._headers("text/html; charset=utf-8"); self.end_headers(); self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求不是有效 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def do_OPTIONS(self):
        self.send_response(204); self._headers(); self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/health":
            with self.bridge.lock:
                state = _safe_state(self.bridge.client)
                state["credentialsLoading"] = bool(self.bridge.credentials_loading)
                state["credentialsError"] = bool(self.bridge.credentials_error)
            return self._json(200, {"ok": True, "state": state})
        if route == "/api/state":
            with self.bridge.lock:
                state = _safe_state(self.bridge.client)
                state["credentialsLoading"] = bool(self.bridge.credentials_loading)
                state["credentialsError"] = bool(self.bridge.credentials_error)
            return self._json(200, state)
        if route == "/api/qr/image":
            image = self.bridge.qr_image
            if not image:
                return self._json(404, {"error": "二维码尚未生成"})
            raw = base64.b64decode(image.split(",", 1)[-1])
            self.send_response(200); self._headers("image/png"); self.end_headers(); self.wfile.write(raw)
            return
        if route == "/api/stats":
            try:
                stats = self.bridge.client.read_stats("monthly")
                notes = self.bridge.client.notebooks()
                return self._json(200, {
                    "stats": stats,
                    "notes": notes,
                    "capability": "reading-data",
                    "source": "weread-gateway",
                })
            except (AuthError, WeFishError, ValueError) as exc:
                return self._json(401 if isinstance(exc, AuthError) else 400, {
                    "code": "READING_DATA_UNAVAILABLE",
                    "capability": "reading-data",
                    "retryable": not isinstance(exc, AuthError),
                    "message": str(exc)[:240],
                })
        if route == "/api/reader/progress":
            book_id = str((parse_qs(parsed.query).get("bookId") or [""])[0]).strip()
            if not book_id:
                return self._json(400, {"code": "BOOK_ID_REQUIRED", "message": "缺少 bookId"})
            try:
                raw, source = _remote_progress(self.bridge.client, book_id)
                node = raw.get("book") if isinstance(raw, dict) and isinstance(raw.get("book"), dict) else raw
                node = node if isinstance(node, dict) else {}
                return self._json(200, {
                    "bookId": book_id,
                    "progress": node.get("progress", node.get("readingProgress", 0)),
                    "chapterUid": node.get("chapterUid", node.get("chapterId")),
                    "chapterIdx": node.get("chapterIdx", node.get("chapterIndex", 0)),
                    "chapterOffset": node.get("chapterOffset", node.get("chapterPos", node.get("offset", 0))),
                    "summary": node.get("summary", node.get("chapterTitle", "")),
                    "elapsed": node.get("readingTime", node.get("elapsed", 0)),
                    "updatedAt": node.get("updatedAt", node.get("updateTime", 0)),
                    "source": source,
                    "capability": "progress-sync",
                })
            except (AuthError, WeFishError, ValueError) as exc:
                return self._json(401 if isinstance(exc, AuthError) else 400, {
                    "code": "PROGRESS_UNAVAILABLE",
                    "capability": "progress-sync",
                    "retryable": not isinstance(exc, AuthError),
                    "message": str(exc)[:240],
                })
        return self._json(404, {"error": "未知接口"})

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            payload = self._body()
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})

        try:
            with self.bridge.lock:
                if route == "/api/qr/begin":
                    self.bridge.credentials_cancelled = True
                    self.bridge.credentials_loading = False
                    self.bridge.credentials_error = False
                    uid = self.bridge.client.qr_begin()
                    self.bridge.qr_uid = uid
                    try:
                        import qrcode
                        image = qrcode.make(f"https://weread.qq.com/web/confirm?uid={uid}")
                        stream = io.BytesIO(); image.save(stream, format="PNG")
                        self.bridge.qr_image = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")
                    except Exception as exc:
                        raise WeFishError(f"二维码生成失败：{exc}") from exc
                    return self._json(200, {"uid": uid, "qrDataUrl": self.bridge.qr_image})

                if route == "/api/qr/poll":
                    uid = str(payload.get("uid") or self.bridge.qr_uid)
                    result = self.bridge.client.qr_poll(uid, otp=str(payload.get("otp") or ""))
                    if result.get("succeed"):
                        account = self.bridge.client.qr_complete(result)
                        return self._json(200, {
                            "login": {"succeed": True},
                            "account": {
                                "user": account.get("user") or {},
                                "needs_skill": bool(account.get("needs_skill")),
                                "persisted": bool(account.get("persisted", True)),
                            },
                            "state": _safe_state(self.bridge.client),
                        })
                    return self._json(200, {"login": result})

                if route == "/api/auth/manual":
                    self.bridge.credentials_cancelled = True
                    self.bridge.credentials_loading = False
                    self.bridge.credentials_error = False
                    api_key = str(payload.get("apiKey") or "").strip()
                    cookie = str(payload.get("cookie") or "").strip()
                    if not api_key and not cookie:
                        raise AuthError("请至少填写 API Key 或 Web Reader Cookie")
                    if api_key:
                        self.bridge.client.session.api_key = api_key
                    if cookie:
                        self.bridge.client.set_cookie_string(cookie)
                    persisted = self.bridge.client.persist_credentials()
                    return self._json(200, {"persisted": persisted, "state": _safe_state(self.bridge.client)})

                if route == "/api/verify":
                    account = self.bridge.client.verify_web_session()
                    return self._json(200, {"ok": True, "accountName": str(account.get("name") or account.get("nickName") or "")})

                if route == "/api/shelf":
                    return self._json(200, self.bridge.client.shelf())

                if route == "/api/reader/import":
                    return self._json(200, self.bridge.client.import_reader_book(str(payload.get("url") or "")))

                if route == "/api/reader/catalog":
                    book_id = str(payload.get("bookId") or "")
                    if not book_id:
                        raise WeFishError("缺少 bookId")
                    if self.bridge.client.is_mp_book(book_id):
                        articles = self.bridge.client.mp_articles(book_id)
                        chapters = [{
                            "chapterUid": row.get("reviewId"),
                            "chapterIdx": index + 1,
                            "title": row.get("title") or f"文章 {index + 1}",
                            "wordCount": 1,
                        } for index, row in enumerate(articles)]
                        return self._json(200, {"bookId": book_id, "chapters": chapters, "entitlement": {}})
                    chapters = self.bridge.client.chapter_catalog(book_id)
                    return self._json(200, {
                        "bookId": book_id,
                        "chapters": chapters,
                        "entitlement": self.bridge.client.entitlement_summary(chapters),
                    })

                if route == "/api/reader/entitlement":
                    book_id = str(payload.get("bookId") or "")
                    if not book_id:
                        raise WeFishError("缺少 bookId")
                    if self.bridge.client.is_mp_book(book_id):
                        return self._json(200, {
                            "bookId": book_id,
                            "membership": {
                                "status": "not-applicable",
                                "source": "public-account",
                                "hasSnapshot": False,
                            },
                            "capability": "membership-sync",
                        })
                    chapter_uid = payload.get("chapterUid")
                    membership = self.bridge.client.reader_membership(book_id, chapter_uid)
                    return self._json(200, {
                        "bookId": book_id,
                        "chapterUid": chapter_uid,
                        "membership": membership,
                        "capability": "membership-sync",
                    })

                if route == "/api/reader/chapter":
                    book_id = str(payload.get("bookId") or "")
                    chapter_uid = payload.get("chapterUid")
                    if self.bridge.client.is_mp_book(book_id):
                        source = self.bridge.client.mp_content(str(chapter_uid), inline_images=True)
                    else:
                        source = self.bridge.client.fetch_chapter(book_id, int(chapter_uid))
                    return self._html(200, source)

                if route == "/api/reader/report":
                    return self._json(200, self.bridge.client.report_read(payload))

                if route == "/api/reader/annotations":
                    return self._json(200, self.bridge.client.annotations(str(payload.get("bookId") or ""), int(payload.get("chapterUid"))))

                if route == "/api/stats":
                    stats = self.bridge.client.read_stats("monthly")
                    notes = self.bridge.client.notebooks()
                    return self._json(200, {"stats": stats, "notes": notes})

                return self._json(404, {"error": "未知接口"})
        except (AuthError, WeFishError, ValueError) as exc:
            return self._json(401 if isinstance(exc, AuthError) else 400, {"error": str(exc)[:240]})
        except Exception as exc:
            return self._json(500, {"error": f"服务异常：{str(exc)[:200]}"})


def main(argv=None):
    parser = argparse.ArgumentParser(description="WeFish localhost bridge")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)

    mock_server = None
    if args.mock:
        from mock_service import serve
        mock_server = serve()
        threading.Thread(target=mock_server.serve_forever, daemon=True).start()
        client = WeReadClient(base_url=f"http://127.0.0.1:{mock_server.server_port}")
    else:
        client = WeReadClient()
    environment_credentials = apply_environment_credentials(client)

    server = Bridge(("127.0.0.1", args.port), client)
    server.mock_server = mock_server
    if not args.mock and not environment_credentials:
        # Keychain/Credential Manager access can wait for an OS prompt or a
        # locked vault. The local bridge must become reachable first so the
        # UI can still offer QR login and manual Cookie authorization.
        server.credentials_loading = True

        def restore_in_background():
            try:
                restored, restore_status = _restore_credentials_with_timeout(client)
                if restore_status != "ok" or restored is None:
                    with server.lock:
                        server.credentials_error = True
                    return
                with server.lock:
                    if not server.credentials_cancelled:
                        client.session = restored.session
                        client.web_opener = restored.web_opener
                        client.qr_cookies = restored.qr_cookies
                        client.qr_opener = restored.qr_opener
            except Exception:
                with server.lock:
                    server.credentials_error = True
            finally:
                with server.lock:
                    server.credentials_loading = False

        threading.Thread(target=restore_in_background, name="wefish-credential-restore", daemon=True).start()
    ready_file = os.environ.get("WEFISH_READY_FILE", "")
    if ready_file:
        with open(ready_file, "w", encoding="utf-8") as handle:
            handle.write(str(server.server_port))
    print(f"WEFISH_PORT={server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if mock_server:
            mock_server.shutdown(); mock_server.server_close()


if __name__ == "__main__":
    main()

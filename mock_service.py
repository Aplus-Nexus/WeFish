"""Synthetic WeRead service for deterministic offline acceptance tests."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import tarfile
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BOOK = {"bookId": "wf-001", "title": "山谷来信", "author": "合成作者", "progress": 12}
MP_BOOK = {"bookId": "MP_WXS_001", "title": "合成公众号", "author": "公众号", "progress": 7}
CHAPTERS = [{"chapterUid": i, "chapterIdx": i, "title": f"第{i}章", "wordCount": 2600, "tar": "https://weread.qq.com/mock-assets/chapter.tar"} for i in range(1, 4)]
MP_ARTICLE = {
    "reviewId": "MP_WXS_001_article-1",
    "reviewIds": ["MP_WXS_001_article-1", "article-1"],
    "originalId": "article-1",
    "bookId": MP_BOOK["bookId"],
    "title": "合成文章",
    "picUrl": "",
    "createTime": 1700000000,
}


def _swap_positions(encoded: str):
    length = len(encoded)
    if length < 4:
        return []
    if length < 11:
        return [0, 2]
    n = min(4, (length + 9) // 10)
    tmp = ""
    for i in range(length - 1, length - n - 1, -1):
        tmp += str(int(bin(ord(encoded[i]))[2:], 4))
    result = []
    m = length - n - 2
    step = len(str(m))
    i = 0
    while len(result) < 10 and i + step < len(tmp):
        result.extend((int(tmp[i:i + step]) % m, int(tmp[i + 1:i + 1 + step]) % m))
        i += step
    return result


def _encoded_shards(text: str, count: int = 3):
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")
    chars = list(encoded)
    positions = _swap_positions(encoded)
    for i in range(0, len(positions), 2):
        for offset in (0, 1):
            chars[positions[i] + offset], chars[positions[i + 1] + offset] = chars[positions[i + 1] + offset], chars[positions[i] + offset]
    payload = "0" + "".join(chars)
    shards = []
    for i in range(count):
        body = payload[len(payload) * i // count:len(payload) * (i + 1) // count]
        shards.append(hashlib.md5(body.encode()).hexdigest().upper() + body)
    return shards


MOCK_PARAGRAPHS = "".join(
    f"<p>合成章节正文，第{i}段。这里是一段足够长的离线阅读内容，用来验证分页、预加载、逐行显示和阅读位置回传。</p>"
    for i in range(1, 48)
)
CONTENT_SHARDS = _encoded_shards("<h1>山谷来信</h1>" + MOCK_PARAGRAPHS + "<img src='../images/leaf.png'/>")
STYLE_SHARD = _encoded_shards("p { line-height: 1.7; }", count=1)[0]


def _asset_tar():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        info = tarfile.TarInfo("images/leaf.png")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


ASSET_TAR = _asset_tar()


def _asset_zip():
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        archive.writestr("images/leaf.png", data)
    return output.getvalue()


ASSET_ZIP = _asset_zip()


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """Avoid a reverse-DNS lookup that can hang packaged offline demos."""
    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "WeFishMock/1"
    def log_message(self, *_):
        return
    def _json(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(raw)
    def _html(self, status, data):
        raw = data.encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(raw)
    def _write_raw(self, status, data):
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.send_response(status); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(length) if length else b"{}"
        try: body = json.loads(raw or b"{}")
        except json.JSONDecodeError: return self._json(400, {"errmsg": "bad json"})
        if self.path == "/api/agent/gateway":
            api = body.get("api_name")
            if api == "/shelf/sync": return self._json(200, {"books": [BOOK], "albums": [], "archive": []})
            if api == "/book/getprogress":
                latest = getattr(self.server, "latest_progress", None)
                book = {"bookId": BOOK["bookId"], "progress": BOOK["progress"], "chapterUid": 1}
                if latest:
                    book.update(latest)
                return self._json(200, {"book": book})
            if api == "/book/info": return self._json(200, BOOK)
            if api == "/book/bookmarklist": return self._json(200, {"updated": [{"markText": "合成摘录", "chapterUid": 1, "range": "0-4"}]})
            if api == "/book/readreviews": return self._json(200, {"reviews": [{"range": "0-4", "pageReviews": [{"review": {"content": "合成书友想法", "chapterUid": 1, "range": "0-4"}}]}]})
            if api == "/review/list/mine": return self._json(200, {"reviews": [{"review": {"content": "合成我的想法", "chapterUid": 1, "reviewId": "mine-1"}}]})
            if api == "/review/list": return self._json(200, {"reviews": [{"content": "合成书评"}]})
            if api == "/readdata/detail": return self._json(200, {"readingTime": 3600, "readingDays": 4, "mode": body.get("mode", "monthly")})
            if api == "/user/notebooks": return self._json(200, {"totalBookCount": 1, "totalNoteCount": 2, "books": [{"bookId": BOOK["bookId"], "noteCount": 2, "reviewCount": 1, "bookmarkCount": 1}]})
            if api == "/store/search": return self._json(200, {"results": [{"books": [{"bookInfo": BOOK}]}]})
            return self._json(501, {"errmsg": "unknown api"})
        if self.path == "/web/login/renewal":
            raw = json.dumps({"succ": 1}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("x-wr-ticket", "mock-ticket")
            self.send_header("x-wrpa-0", "mock-wrpa")
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path == "/web/book/read":
            if hasattr(self.server, "read_requests"):
                self.server.read_requests.append(body)
            self.server.latest_progress = {
                "progress": body.get("pr", body.get("progress", BOOK["progress"])),
                "chapterUid": body.get("chapterUid", 1),
                "chapterIdx": body.get("ci", body.get("chapterIdx", 1)),
                "chapterOffset": body.get("co", body.get("chapterOffset", 0)),
                "summary": body.get("sm", body.get("summary", "")),
                "readingTime": int(getattr(self.server, "reading_time", 0)) + int(body.get("rt", body.get("elapsed", 0)) or 0),
                "updatedAt": 1700000000 + len(getattr(self.server, "read_requests", [])),
            }
            self.server.reading_time = self.server.latest_progress["readingTime"]
            return self._json(200, {"succ": 1})
        if self.path == "/web/book/chapterInfos":
            return self._json(200, [{"bookId": BOOK["bookId"], "synckey": "catalog-key", "copyRightSynckey": "copyright-key", "format": "epub", "updated": CHAPTERS}])
        if self.path.startswith("/web/book/chapter/"):
            shard = self.path.rsplit("/", 1)[-1]
            if hasattr(self.server, "content_requests"):
                self.server.content_requests.append({"method": "POST", "shard": shard, "body": body})
            if shard == "e_0": return self._write_raw(200, CONTENT_SHARDS[0])
            if shard == "e_1": return self._write_raw(200, CONTENT_SHARDS[1])
            if shard == "e_3": return self._write_raw(200, CONTENT_SHARDS[2])
            if shard == "e_2": return self._write_raw(200, STYLE_SHARD)
            return self._json(501, {"errmsg": "unknown chapter shard"})
        return self._json(501, {"errmsg": "unknown path"})
    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/r/weread-skills": return self._json(200, {"ok": True})
        if route == "/web/shelf/sync":
            row = dict(BOOK)
            row["chapterUid"] = 1
            return self._json(200, {"books": [row], "albums": [], "archive": []})
        if route == "/api/auth/getLoginUid": return self._json(200, {"uid": "mock-uid"})
        if route == "/api/auth/getLoginInfo": return self._json(200, {"succeed": True, "webLoginVid": "mock-vid", "accessToken": "mock-skey", "refreshToken": "mock-refresh"})
        if route == "/api/userInfo": return self._json(200, {"name": "合成账号", "userVid": "mock-vid"})
        if route == "/api/skills/apikeyGet":
            if getattr(self.server, "apikey_unauthorized", False):
                return self._json(401, {"errmsg": "Cgi Request Error"})
            return self._json(200, {"apikey": "wrk-mock-key"})
        if route.startswith("/web/mp/reader/"):
            state = {"reader": {"bookInfo": MP_BOOK, "currentArticle": {"reviewId": MP_ARTICLE["reviewId"]}, "progress": {"progress": MP_BOOK["progress"]}}}
            return self._html(200, "<html><script>window.__INITIAL_STATE__ = " + json.dumps(state, ensure_ascii=False) + "; (function(){})();</script></html>")
        if route.startswith("/web/reader/"):
            latest = getattr(self.server, "latest_progress", None) or {}
            state = {"reader": {
                "bookInfo": BOOK,
                "psvts": "mock-psvts",
                "pclts": "mock-pclts",
                "token": "mock-reader-token",
                "currentChapter": {"chapterUid": 1},
                "progress": {
                    "progress": latest.get("progress", 12),
                    "chapterOffset": latest.get("chapterOffset", 60),
                    "summary": latest.get("summary", "合成章节正文"),
                    "readingTime": latest.get("readingTime", 0),
                },
                "memberCardSummary": {
                    "isMember": True,
                    "remainTime": 86400 * 30,
                    "expiredTime": 1800000000,
                    "cardType": "mock-unlimited",
                },
            }}
            return self._html(200, "<html><script>window.__INITIAL_STATE__ = " + json.dumps(state, ensure_ascii=False) + "; (function(){})();</script></html>")
        if route == "/web/mp/articles":
            return self._json(200, {"reviews": [{"subReviews": [{"review": {
                "reviewId": MP_ARTICLE["reviewId"], "belongBookId": MP_BOOK["bookId"],
                "createTime": MP_ARTICLE["createTime"], "mpInfo": {
                    "originalId": MP_ARTICLE["originalId"], "title": MP_ARTICLE["title"],
                    "pic_url": MP_ARTICLE["picUrl"],
                },
            }}]}], "synckey": 1700000000})
        if route == "/web/mp/content":
            return self._html(200, """<html><body><div id='js_content'><p>公众号正文。</p><div><span>嵌套内容</span></div><script>bad()</script></div><div class='rich_media_content'>备用内容</div></body></html>""")
        if route == "/mock-assets/chapter.tar":
            return self._write_raw(200, ASSET_TAR)
        if route == "/mock-assets/chapter.zip":
            return self._write_raw(200, ASSET_ZIP)
        if route.startswith("/web/book/chapter/"):
            shard = route.rsplit("/", 1)[-1]
            if shard in {"e_0", "e_1", "e_3"}:
                return self._json(200, {"html": "<h1>山谷来信</h1><p>合成章节正文。</p>"})
        return self._json(501, {"errmsg": "unknown path"})


def serve(port=0):
    server = LocalThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.content_requests = []
    server.read_requests = []
    server.apikey_unauthorized = False
    server.latest_progress = None
    server.reading_time = 0
    return server


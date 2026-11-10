import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, build_opener

from wefish_core import AuthError, CredentialVault, LocalCache, WeReadClient, WeFishError, apply_environment_credentials, content_params, decode_content_shards, extract_mp_body, extract_reader_state, read_report_params
from mock_service import _encoded_shards, serve


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve(); cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def client(self):
        return WeReadClient(base_url=self.base, opener=build_opener())

    def test_credentials_round_trip_through_os_vault_adapter(self):
        class MemoryKeyring:
            value = None
            def get_password(self, service, account):
                return self.value
            def set_password(self, service, account, value):
                self.value = value

        backend = MemoryKeyring()
        first = WeReadClient(base_url=self.base, opener=build_opener(), vault=CredentialVault(backend))
        first.session.api_key = "wrk-test-key"
        first.set_cookie_string("wr_vid=test-vid; wr_skey=test-skey")
        first.session.device_fingerprint = "123456789"
        first.session.user_info = {"name": "测试账号", "userVid": "vid-1"}
        first.session.web_headers = {"x-wr-ticket": "ticket", "x-wrpa-0": "wrpa"}
        self.assertTrue(first.persist_credentials())

        second = WeReadClient(base_url=self.base, opener=build_opener(), vault=CredentialVault(backend))
        self.assertTrue(second.restore_credentials())
        self.assertEqual(second.session.api_key, "wrk-test-key")
        self.assertEqual(second.session.device_fingerprint, "123456789")
        self.assertEqual(second.session.user_info["name"], "测试账号")
        self.assertTrue(any(cookie.name == "wr_skey" and cookie.value == "test-skey" for cookie in second.session.cookies))
        self.assertEqual(second.session.web_headers, {"x-wr-ticket": "ticket", "x-wrpa-0": "wrpa"})

    def test_environment_credentials_are_applied_without_persistence(self):
        client = self.client()
        self.assertTrue(apply_environment_credentials(client, {
            "WEREAD_API_KEY": "wrk-env-key",
            "WEREAD_COOKIE": "wr_vid=env-vid; wr_skey=env-skey",
        }))
        self.assertEqual(client.session.api_key, "wrk-env-key")
        self.assertTrue(any(cookie.name == "wr_skey" and cookie.value == "env-skey" for cookie in client.session.cookies))
    def test_shelf_progress_annotations_and_reading(self):
        c = self.client()
        shelf = c.shelf(); self.assertEqual(shelf["books"][0]["bookId"], "wf-001")
        self.assertEqual(c.progress("wf-001")["book"]["progress"], 12)
        annotations = c.annotations("wf-001", 1)
        self.assertEqual(annotations["underlines"][0]["markText"], "合成摘录")
        self.assertEqual(annotations["reviews"][0]["content"], "合成书友想法")
        self.assertEqual(annotations["my_reviews"][0]["content"], "合成我的想法")
        self.assertIn("合成章节正文", c.read_chapter("b", "c"))
        self.assertEqual(len(c.chapter_catalog("wf-001")), 3)
        self.assertIn("合成章节正文", c.fetch_chapter("wf-001", 1))
        self.assertEqual(c.read_stats("monthly")["readingTime"], 3600)
        self.assertEqual(c.notebooks()["totalNoteCount"], 2)
        self.assertEqual(c.report_read({"bookId": "wf-001", "elapsed": 30})["succ"], 1)
        self.assertTrue(c.renew())
        self.assertEqual(c.session.web_headers["x-wr-ticket"], "mock-ticket")
        self.assertEqual(c.session.web_headers["x-wrpa-0"], "mock-wrpa")
    def test_api_key_required_for_real_gateway(self):
        with self.assertRaises(AuthError): WeReadClient().shelf()

    def test_web_cookie_can_sync_shelf_without_skill_key(self):
        c = self.client()
        c.set_cookie_string("wr_vid=mock-vid; wr_skey=mock-skey")
        shelf = c.web_shelf()
        self.assertEqual(shelf["books"][0]["bookId"], "wf-001")

    def test_api_key_alone_cannot_read_web_content(self):
        c = WeReadClient()
        c.session.api_key = "wrk-only-gateway-key"
        with self.assertRaisesRegex(AuthError, "Web Reader Cookie"):
            c.reader_state("43208843", 1)

    def test_web_session_verification_returns_public_account_data(self):
        c = self.client()
        c.set_cookie_string("wr_vid=mock-vid; wr_skey=mock-skey")
        account = c.verify_web_session()
        self.assertEqual(account["name"], "合成账号")
        self.assertEqual(c.session.user_info["userVid"], "mock-vid")

    def test_gateway_and_web_transport_use_separate_openers(self):
        c = self.client()
        self.assertIsNot(c.gateway_opener, c.web_opener)
        self.assertNotIn("Cookie", str(c.gateway_opener))

    def test_qr_login_exchange_sets_cookie_and_api_key(self):
        c = self.client(); uid = c.qr_begin(); self.assertEqual(uid, "mock-uid")
        result = c.qr_complete(c.qr_poll(uid)); self.assertEqual(result["user"]["name"], "合成账号")
        self.assertEqual(c.session.api_key, "wrk-mock-key")
        self.assertEqual(c.session.user_info["name"], "合成账号")

    def test_qr_login_replaces_previous_account_session(self):
        c = self.client()
        c.set_cookie_string("wr_vid=old-vid; wr_skey=old-skey")
        c.session.web_headers = {"x-wr-ticket": "old-ticket"}
        uid = c.qr_begin()
        c.qr_complete(c.qr_poll(uid))
        cookies = {cookie.name: cookie.value for cookie in c.session.cookies}
        self.assertEqual(cookies["wr_vid"], "mock-vid")
        self.assertEqual(cookies["wr_skey"], "mock-skey")
        self.assertNotEqual(cookies["wr_skey"], "old-skey")
        self.assertEqual(c.session.web_headers, {})

    def test_qr_poll_pending_is_not_an_auth_failure(self):
        c = self.client()
        # A mock transport timeout is represented the same way as WeRead's
        # long-poll timeout: the UI should keep the QR dialog open.
        original = c._request
        def pending(*args, **kwargs): raise WeFishError("network error after 1 attempts")
        c._request = pending
        self.assertTrue(c.qr_poll("mock-uid")["transport_pending"])
        c._request = original

    def test_qr_poll_401_cgi_error_stays_pending(self):
        c = self.client()
        def rejected(*args, **kwargs):
            raise AuthError("HTTP 401: Cgi Request Error (-2013)")
        c._request = rejected
        result = c.qr_poll("mock-uid")
        self.assertTrue(result["transport_pending"])
        self.assertTrue(result["poll_auth_pending"])

    def test_qr_poll_forwards_four_digit_otp_without_logging_it(self):
        c = self.client()
        seen = {}
        def capture(method, url, *args, **kwargs):
            seen["url"] = url
            return json.dumps({"logicCode": "NEED_OTP"}).encode()
        c._request = capture
        result = c.qr_poll("mock-uid", otp="1234")
        self.assertEqual(result["logicCode"], "NEED_OTP")
        self.assertIn("uid=mock-uid", seen["url"])
        self.assertIn("otp=1234", seen["url"])

    def test_qr_success_can_keep_web_cookie_without_skill_key(self):
        c = self.client()
        original = c._request
        def no_skill(method, url, *args, **kwargs):
            if "/api/userInfo" in url:
                return json.dumps({"name": "无 Skill 账号"}).encode()
            if "/api/skills/apikeyGet" in url:
                return b"{}"
            return original(method, url, *args, **kwargs)
        c._request = no_skill
        result = c.qr_complete({"succeed": True, "webLoginVid": "vid-no-skill", "accessToken": "skey-no-skill"})
        self.assertTrue(result["needs_skill"])
        self.assertFalse(result["api_key"])
        self.assertTrue(c.session.cookies)

    def test_qr_skill_http_401_is_cookie_only_login(self):
        self.server.apikey_unauthorized = True
        try:
            c = self.client()
            uid = c.qr_begin()
            result = c.qr_complete(c.qr_poll(uid))
            self.assertTrue(result["needs_skill"])
            self.assertFalse(c.session.api_key)
            self.assertTrue(any(cookie.name == "wr_skey" for cookie in c.session.cookies))
            self.assertFalse(result["persisted"])
            account = c.verify_web_session()
            self.assertEqual(account["name"], "合成账号")
            book = c.import_reader_book("https://weread.qq.com/web/reader/mock-book")
            self.assertEqual(book["bookId"], "wf-001")
            self.assertIn("合成章节正文", c.fetch_chapter(book["bookId"], book["chapterUid"]))
        finally:
            self.server.apikey_unauthorized = False

    def test_qr_login_preserves_token_cookie_values(self):
        c = self.client()
        original = c._request
        def token_response(method, url, *args, **kwargs):
            if "/api/userInfo" in url:
                return json.dumps({"name": "特殊令牌账号"}).encode()
            if "/api/skills/apikeyGet" in url:
                return b"{}"
            return original(method, url, *args, **kwargs)
        c._request = token_response
        c.qr_complete({
            "succeed": True,
            "webLoginVid": "vid/with+chars",
            "accessToken": "skey/with+chars==",
            "refreshToken": "refresh/with+chars==",
        })
        cookies = {cookie.name: cookie.value for cookie in c.session.cookies}
        self.assertEqual(cookies["wr_vid"], "vid/with+chars")
        self.assertEqual(cookies["wr_skey"], "skey/with+chars==")
        self.assertEqual(cookies["wr_rt"], "refresh/with+chars==")

    def test_qr_cookie_is_persisted_before_skill_key_failure(self):
        class MemoryKeyring:
            value = None
            def get_password(self, service, account):
                return self.value
            def set_password(self, service, account, value):
                self.value = value

        backend = MemoryKeyring()
        c = WeReadClient(vault=CredentialVault(backend))

        def key_failure(method, url, *args, **kwargs):
            if "/api/userInfo" in url:
                return json.dumps({"name": "Cookie 保留账号"}).encode()
            if "/api/skills/apikeyGet" in url:
                raise AuthError("temporary unauthorized")
            return b"{}"

        c._request = key_failure
        result = c.qr_complete({"succeed": True, "webLoginVid": "vid-cookie", "accessToken": "skey-cookie"})
        self.assertTrue(result["needs_skill"])
        saved = CredentialVault(backend).load()
        self.assertFalse(saved.get("api_key"))
        self.assertTrue(any(row.get("name") == "wr_skey" for row in saved.get("cookies", [])))

    def test_api_key_extractor_accepts_nested_skill_response(self):
        self.assertEqual(WeReadClient._extract_api_key({"data": {"api_key": "wrk-nested"}}), "wrk-nested")
        self.assertEqual(WeReadClient._extract_api_key({"result": {"apiKey": "wrk-result"}}), "wrk-result")
    def test_retry_error_is_public_and_cache_is_atomic(self):
        c = self.client();
        with self.assertRaises(WeFishError) as ctx: c.gateway("/unknown")
        self.assertNotIn("Authorization", str(ctx.exception)); self.assertNotIn("Cookie", str(ctx.exception))
        with TemporaryDirectory() as d:
            cache = LocalCache(Path(d)); cache.put("book-1", {"title": "测试"}); self.assertEqual(cache.get("book-1")["title"], "测试")

    def test_real_style_shard_decoder_round_trip(self):
        # The decoder is kept compatible with the reference plugin's shard
        # contract; this fixture is generated locally and contains no book.
        import base64, hashlib
        plain = "<p>合成 UTF-8 内容 🌿</p>"
        encoded = base64.urlsafe_b64encode(plain.encode()).decode().rstrip("=")
        def positions_for(value):
            length = len(value); n = min(4, (length + 9) // 10); tmp = "".join(str(int(bin(ord(value[i]))[2:], 4)) for i in range(length - 1, length - n - 1, -1)); result=[]; m=length-n-2; step=len(str(m)); i=0
            while len(result) < 10 and i + step < len(tmp): result.extend((int(tmp[i:i+step]) % m, int(tmp[i+1:i+1+step]) % m)); i += step
            return result
        chars = list(encoded); positions = positions_for(encoded)
        for i in range(0, len(positions), 2):
            for k in (0, 1): chars[positions[i] + k], chars[positions[i+1] + k] = chars[positions[i+1] + k], chars[positions[i] + k]
        scrambled = "0" + "".join(chars)
        shards = [scrambled[len(scrambled) * i // 3:len(scrambled) * (i + 1) // 3] for i in range(3)]
        shards = [(hashlib.md5(s.encode()).hexdigest().upper() + s) for s in shards]
        self.assertEqual(decode_content_shards(*shards), plain)

    def test_web_reader_content_bridge_signs_full_chapter_request(self):
        params = content_params("wf-001", 2, "catalog-key")
        self.assertEqual(params["sc"], 1)
        self.assertEqual(params["ps"], "catalog-key")
        self.assertTrue(params["pc"])
        self.assertTrue(params["s"])

    def test_web_reader_content_bridge_uses_reader_psvts_and_post_shards(self):
        c = self.client()
        source = c.fetch_chapter("wf-001", 1)
        self.assertIn("合成章节正文", source)
        requests = self.server.content_requests
        recent = requests[-4:]
        self.assertEqual([item["shard"] for item in recent], ["e_0", "e_1", "e_3", "e_2"])
        self.assertIn("data:image/png;base64", source)
        self.assertIn("line-height", source)
        for item in recent:
            self.assertEqual(item["method"], "POST")
            self.assertEqual(item["body"]["ps"], "mock-psvts")
            self.assertEqual(item["body"]["sc"], 1)
            self.assertTrue(item["body"]["pc"])
            self.assertTrue(item["body"]["s"])
        self.assertEqual(recent[-1]["body"]["st"], 1)
        self.assertTrue(all(item["body"]["st"] == 0 for item in recent[:-1]))

        c.report_read({"bookId": "wf-001", "chapterUid": 1, "chapterIdx": 1, "chapterOffset": 42, "summary": "章节摘要", "progress": 12, "elapsed": 30})
        report = self.server.read_requests[-1]
        for field in ("b", "c", "ci", "co", "pr", "rt", "ts", "rn", "sg", "ct", "ps", "pc", "s"):
            self.assertIn(field, report)
        self.assertEqual(report["ps"], "mock-psvts")
        self.assertEqual(report["rt"], 30)
        self.assertEqual(report["co"], 42)
        self.assertEqual(report["sm"], "章节摘要")

    def test_reader_report_does_not_invent_a_token_when_reader_state_has_none(self):
        params = read_report_params(
            book_id="wf-001",
            chapter_uid=1,
            psvts="mock-psvts",
            token="",
        )
        self.assertNotIn("sg", params)

    def test_reader_content_falls_back_to_txt_shards(self):
        c = self.client()
        txt_shards = _encoded_shards("<p>合成网文正文。</p>", count=2)
        original = c._request

        def txt_request(method, url, *args, **kwargs):
            shard = url.rsplit("/", 1)[-1]
            if shard == "e_0":
                return b'{"bookId":"wf-txt-001","format":"txt"}'
            if shard == "t_0":
                return txt_shards[0].encode()
            if shard == "t_1":
                return txt_shards[1].encode()
            return original(method, url, *args, **kwargs)

        c._request = txt_request
        try:
            self.assertIn("合成网文正文", c.read_chapter("book-hash", "chapter-hash", params={"ps": "ps"}))
        finally:
            c._request = original

    def test_reader_resource_proxy_resolves_relative_zip_assets(self):
        c = self.client()
        source = c._embed_chapter_assets(
            "<p>插图</p><img src='../images/leaf.png'/>",
            {"tar": "/mock-assets/chapter.zip"},
        )
        self.assertIn("data:image/png;base64,", source)

    def test_reader_link_can_open_without_gateway_api_key(self):
        c = self.client()
        book = c.import_reader_book("https://weread.qq.com/web/reader/mock-book")
        self.assertFalse(c.session.api_key)
        self.assertEqual(book["bookId"], "wf-001")
        self.assertEqual(book["chapterUid"], 1)
        self.assertEqual(book["chapterOffset"], 60)
        self.assertEqual(len(book["chapters"]), 3)
        self.assertIn("合成章节正文", c.fetch_chapter(book["bookId"], book["chapterUid"]))

    def test_cookie_only_reader_progress_returns_chapter_offset(self):
        c = self.client()
        c.set_cookie_string("wr_vid=mock-vid; wr_skey=mock-skey")
        progress = c.reader_progress("wf-001", 1)
        self.assertEqual(progress["chapterUid"], 1)
        self.assertEqual(progress["chapterOffset"], 60)

    def test_reader_link_rejects_non_weread_url(self):
        with self.assertRaises(WeFishError):
            self.client().import_reader_book("https://example.com/web/reader/book")

    def test_public_account_reader_uses_cookie_article_flow(self):
        c = self.client()
        self.assertTrue(c.is_mp_book("MP_WXS_001"))
        self.assertFalse(c.is_mp_book("wf-001"))
        articles = c.mp_articles("MP_WXS_001")
        self.assertEqual(articles[0]["reviewId"], "MP_WXS_001_article-1")
        self.assertEqual(articles[0]["title"], "合成文章")
        source = c.mp_content(articles[0]["reviewId"], inline_images=False)
        self.assertIn("公众号正文", source)
        self.assertIn("嵌套内容", source)
        self.assertNotIn("bad()", source)
        imported = c.import_reader_book("https://weread.qq.com/web/mp/reader/mock-mp")
        self.assertTrue(imported["isMp"])
        self.assertEqual(imported["chapterUid"], "MP_WXS_001_article-1")
        self.assertEqual(len(imported["articles"]), 1)

    def test_public_account_body_extractor_handles_nested_divs(self):
        source = extract_mp_body("<div id='js_content'><div><p>一</p></div><script>secret()</script><p>二</p></div>")
        self.assertEqual(source, "<div><p>一</p></div><p>二</p>")

    def test_reader_state_parser_accepts_changed_script_tail(self):
        html = '<script>window.__INITIAL_STATE__ = {"reader":{"psvts":"ps-1","bookInfo":{"bookId":"b-1"}}}; window.__NEXT_DATA__ = {};</script>'
        state = extract_reader_state(html)
        self.assertEqual(state["reader"]["psvts"], "ps-1")
        self.assertEqual(state["reader"]["bookInfo"]["bookId"], "b-1")

    def test_entitlement_summary_uses_server_catalog_flags(self):
        summary = WeReadClient.entitlement_summary([
            {"chapterUid": 1, "paid": True, "price": 0},
            {"chapterUid": 2, "paid": False, "price": 3.5},
            {"chapterUid": 3, "paid": False, "price": 0},
        ])
        self.assertTrue(summary["has_paid_content"])
        self.assertEqual(summary["paid_chapters"], 2)
        self.assertEqual(summary["prices"], [3.5])

    def test_membership_summary_normalizes_web_reader_snapshot(self):
        summary = WeReadClient.membership_summary({
            "reader": {
                "memberCardSummary": {
                    "isMember": True,
                    "remainTime": 3600,
                    "cardType": "unlimited",
                },
            },
        })
        self.assertEqual(summary["status"], "active")
        self.assertEqual(summary["remainingSeconds"], 3600)
        self.assertEqual(summary["cardType"], "unlimited")
        self.assertTrue(summary["hasSnapshot"])

    def test_membership_summary_does_not_guess_from_empty_snapshot(self):
        summary = WeReadClient.membership_summary({"reader": {"memberCardSummary": {}}})
        self.assertEqual(summary["status"], "unknown")
        self.assertFalse(summary["hasSnapshot"])

    def test_mp_public_images_do_not_receive_reader_session_headers(self):
        c = self.client()
        calls = []
        original = c._request

        def image_request(method, url, *args, **kwargs):
            calls.append((url, kwargs.get("web_session")))
            return b"fake-image"

        c._request = image_request
        source = c._inline_mp_images("<p><img src='https://mmbiz.qpic.cn/a/photo.jpg'/></p>")
        c._request = original
        self.assertIn("data:image/jpeg;base64,", source)
        self.assertEqual(calls, [("https://mmbiz.qpic.cn/a/photo.jpg", False)])


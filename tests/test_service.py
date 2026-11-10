import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wefish_core import AuthError
from wefish_service import _remote_progress, _restore_credentials_with_timeout


class ServiceCredentialRestoreTests(unittest.TestCase):
    def test_cookie_only_progress_falls_back_to_web_reader_snapshot(self):
        class CookieOnlyClient:
            def progress(self, _book_id):
                raise AuthError("gateway key required")

            def web_shelf(self):
                return {"books": [{"bookId": "book-1", "chapterUid": 7, "progress": 12}]}

            def reader_progress(self, _book_id, chapter_uid):
                self.chapter_uid = chapter_uid
                return {"chapterUid": chapter_uid, "chapterOffset": 88, "readingTime": 42}

        client = CookieOnlyClient()
        payload, source = _remote_progress(client, "book-1")
        self.assertEqual(source, "weread-web-reader")
        self.assertEqual(payload["book"]["chapterOffset"], 88)
        self.assertEqual(client.chapter_uid, 7)

    def test_restore_timeout_returns_without_waiting_for_locked_vault(self):
        class SlowClient:
            def __init__(self, **_kwargs):
                pass

            def restore_credentials(self):
                time.sleep(0.2)

        started = time.monotonic()
        with patch("wefish_service.WeReadClient", SlowClient):
            restored, status = _restore_credentials_with_timeout(SimpleNamespace(timeout=1, vault=object()), timeout=0.02)
        self.assertIsNone(restored)
        self.assertEqual(status, "timeout")
        self.assertLess(time.monotonic() - started, 0.12)

    def test_restore_success_returns_the_isolated_client(self):
        class FastClient:
            def __init__(self, **_kwargs):
                pass

            def restore_credentials(self):
                return True

        with patch("wefish_service.WeReadClient", FastClient):
            restored, status = _restore_credentials_with_timeout(SimpleNamespace(timeout=1, vault=object()), timeout=0.2)
        self.assertIsInstance(restored, FastClient)
        self.assertEqual(status, "ok")


if __name__ == "__main__":
    unittest.main()

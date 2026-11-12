import json
import subprocess
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ReaderV1ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = subprocess.Popen(
            [sys.executable, "wefish_service.py", "--mock"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = cls.process.stdout.readline().strip()
        if not line.startswith("WEFISH_PORT="):
            raise RuntimeError(f"service did not start: {line}")
        cls.base = f"http://127.0.0.1:{line.split('=', 1)[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)

    def request(self, method, route, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base + route,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        with urlopen(request, timeout=3) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}

    def test_reading_data_get_contract_is_real(self):
        status, payload = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(payload["capability"], "reading-data")
        self.assertEqual(payload["stats"]["readingTime"], 3600)
        self.assertEqual(payload["notes"]["totalNoteCount"], 2)

    def test_weekly_reading_data_and_annotation_writes_use_cloud_contract(self):
        status, weekly = self.request("GET", "/api/stats?mode=weekly")
        self.assertEqual(status, 200)
        self.assertEqual(weekly["mode"], "weekly")
        self.assertEqual(weekly["stats"]["mode"], "weekly")

        status, highlight = self.request("POST", "/api/reader/highlight", {
            "bookId": "wf-001", "chapterUid": 1, "range": "4-10", "markText": "合成当前行",
        })
        self.assertEqual(status, 200)
        self.assertEqual(highlight["succ"], 1)

        status, thought = self.request("POST", "/api/reader/thought", {
            "bookId": "wf-001", "chapterUid": 1, "range": "4-10", "content": "合成想法",
        })
        self.assertEqual(status, 200)
        self.assertEqual(thought["succ"], 1)

    def test_progress_and_duration_round_trip_through_new_session_endpoint(self):
        self.request("POST", "/api/auth/manual", {"cookie": "wr_vid=mock-vid; wr_skey=mock-skey"})
        status, report = self.request("POST", "/api/reader/report", {
            "bookId": "wf-001",
            "chapterUid": 1,
            "chapterIdx": 1,
            "chapterOffset": 123,
            "progress": 20,
            "summary": "分页位置",
            "elapsed": 37,
        })
        self.assertEqual(status, 200)
        self.assertEqual(report["succ"], 1)

        status, remote = self.request("GET", "/api/reader/progress?" + urlencode({"bookId": "wf-001"}))
        self.assertEqual(status, 200)
        self.assertEqual(remote["chapterUid"], 1)
        self.assertEqual(remote["chapterOffset"], 123)
        self.assertEqual(remote["elapsed"], 37)
        self.assertEqual(remote["capability"], "progress-sync")

    def test_mock_chapter_is_long_enough_to_exercise_pages(self):
        self.request("POST", "/api/auth/manual", {"cookie": "wr_vid=mock-vid; wr_skey=mock-skey"})
        status, _ = self.request("POST", "/api/reader/catalog", {"bookId": "wf-001"})
        self.assertEqual(status, 200)
        request = Request(self.base + "/api/reader/chapter", data=json.dumps({"bookId": "wf-001", "chapterUid": 1}).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            source = response.read().decode("utf-8")
        self.assertGreater(len(source), 2500)
        self.assertIn("合成章节正文", source)

    def test_membership_snapshot_is_read_from_web_reader_state(self):
        self.request("POST", "/api/auth/manual", {"cookie": "wr_vid=mock-vid; wr_skey=mock-skey"})
        status, payload = self.request("POST", "/api/reader/entitlement", {"bookId": "wf-001", "chapterUid": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload["capability"], "membership-sync")
        self.assertEqual(payload["membership"]["status"], "active")
        self.assertEqual(payload["membership"]["remainingSeconds"], 86400 * 30)

    def test_unknown_reading_data_route_is_not_used(self):
        with self.assertRaises(HTTPError) as error:
            self.request("GET", "/api/does-not-exist")
        self.assertEqual(error.exception.code, 404)

    def test_progress_requires_a_book_id_with_structured_error(self):
        with self.assertRaises(HTTPError) as error:
            self.request("GET", "/api/reader/progress")
        self.assertEqual(error.exception.code, 400)
        payload = json.loads(error.exception.read().decode("utf-8"))
        self.assertEqual(payload["code"], "BOOK_ID_REQUIRED")

    def test_stats_post_compatibility_matches_get_payload(self):
        _, get_payload = self.request("GET", "/api/stats")
        _, post_payload = self.request("POST", "/api/stats", {})
        self.assertEqual(get_payload["stats"], post_payload["stats"])
        self.assertEqual(get_payload["notes"], post_payload["notes"])


if __name__ == "__main__":
    unittest.main()

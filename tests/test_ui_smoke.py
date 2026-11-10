import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import threading
import time
import unittest

from PySide6.QtWidgets import QApplication, QListWidgetItem, QPushButton, QLineEdit

from app import WeFishWindow, shortcut_modifier_for
from mock_service import serve
from wefish_core import WeReadClient


class DesktopSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.server = serve()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.window = WeFishWindow(WeReadClient(base_url=f"http://127.0.0.1:{self.server.server_port}"))
        self.assertTrue(str(self.window.cache.root).endswith(".wefish/mock-cache"))
        self.wait_for(lambda: self.window.shelf.count() == 1)

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.qt.processEvents()
        self.assertTrue(predicate())

    def tearDown(self):
        self.window.tray.hide(); self.window.close(); self.qt.processEvents()

    def test_shelf_reader_and_mini_mode(self):
        self.assertEqual(self.window.shelf.count(), 1)
        self.window.open_book(self.window.shelf.item(0)); self.qt.processEvents()
        self.wait_for(lambda: "合成章节正文" in self.window.reader.toHtml())
        self.window.toggle_compact(); self.qt.processEvents()
        self.assertTrue(self.window.mini_mode)
        self.assertLessEqual(self.window.width(), 420)
        self.assertLess(self.window.windowOpacity(), 0.95)
        self.window.toggle_compact()
        self.assertFalse(self.window.mini_mode)

    def test_shortcut_modifier_matches_platform(self):
        expected = "Meta" if sys.platform == "darwin" else "Ctrl"
        self.assertEqual(self.window.shortcut_modifier, expected)
        self.assertIn(expected, self.window.hide_shortcut.key().toString())
        self.assertEqual(shortcut_modifier_for("darwin"), "Meta")
        self.assertEqual(shortcut_modifier_for("win32"), "Ctrl")

    def test_global_hotkey_actions_are_marshaled_through_qt(self):
        self.window.globalAction.emit("toggle")
        self.qt.processEvents()
        self.assertTrue(self.window.isVisible())
        self.window.globalAction.emit("toggle")
        self.qt.processEvents()
        self.assertFalse(self.window.isVisible())

    def test_office_title_mode_and_tray_controls(self):
        self.window.toggle_office_title()
        self.assertEqual(self.window.windowTitle(), "工作文档")
        self.assertTrue(self.window.office_title_mode)
        self.window.toggle_office_title()
        self.assertEqual(self.window.windowTitle(), "上班墨鱼 · WeFish")
        labels = [action.text() for action in self.window.tray.contextMenu().actions()]
        self.assertIn("上一章", labels)
        self.assertIn("下一章", labels)
        self.assertIn("微信扫码", labels)
        self.assertIn("关于 WeFish", labels)
        self.assertEqual(self.window.notes_button.text(), "摘录/笔记")

    def test_tray_exit_path_really_requests_application_quit(self):
        self.window.quit_app()
        self.assertTrue(self.window._quit_requested)
        self.assertFalse(self.window.tray.isVisible())
        self.assertFalse(self.window.read_timer.isActive())

    def test_qr_dialog_has_in_place_retry_action(self):
        self.window.start_qr_login()
        dialog = self.window.qr_dialog
        self.assertTrue(dialog.isVisible())
        labels = [button.text() for button in dialog.findChildren(QPushButton)]
        self.assertIn("重新获取", labels)
        self.assertTrue(any(field.maxLength() == 4 for field in dialog.findChildren(QLineEdit)))
        dialog.reject()
        self.qt.processEvents()

    def test_reader_scroll_updates_offset_and_progress(self):
        self.window.show()
        self.window.open_book(self.window.shelf.item(0))
        self.wait_for(lambda: "合成章节正文" in self.window.reader.toHtml())
        self.window.reader.setHtml("<p>" + ("长文本内容 " * 1200) + "</p>")
        self.qt.processEvents()
        bar = self.window.reader.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.qt.processEvents()
        self.assertGreater(self.window.current_chapter_offset, 0)
        self.assertGreaterEqual(self.window.progress.value(), 30)

    def test_slow_shelf_request_does_not_block_window_construction(self):
        class SlowClient(WeReadClient):
            def shelf(self):
                time.sleep(0.25)
                return super().shelf()

        window = WeFishWindow(SlowClient(base_url=f"http://127.0.0.1:{self.server.server_port}"))
        try:
            self.assertEqual(window.shelf.count(), 0)
            self.wait_for(lambda: window.shelf.count() == 1)
        finally:
            window.tray.hide(); window.close(); self.qt.processEvents()

    def test_public_account_item_uses_article_reader(self):
        item = QListWidgetItem("合成公众号")
        item.setData(256, {"bookId": "MP_WXS_001", "title": "合成公众号", "author": "公众号", "isMp": True})
        self.window.open_book(item)
        self.wait_for(lambda: "公众号正文" in self.window.reader.toHtml())
        self.assertEqual(self.window.current_article_id, "MP_WXS_001_article-1")
        self.assertEqual(self.window.article_sequence, ["MP_WXS_001_article-1"])


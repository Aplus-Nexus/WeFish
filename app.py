"""WeFish desktop shell: bookshelf, reader, tray menu and compact mode."""
from __future__ import annotations

import argparse
import io
import html
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal, QObject, Slot, QPoint
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSlider,
    QSystemTrayIcon, QTextBrowser, QVBoxLayout, QWidget, QInputDialog, QLineEdit,
    QDialog, QDialogButtonBox, QFormLayout,
)

from wefish_core import AuthError, LocalCache, WeReadClient, WeFishError


def shortcut_modifier_for(platform_name=None):
    """Return the native modifier used by Qt and pynput on a platform."""
    return "Meta" if (platform_name or sys.platform) == "darwin" else "Ctrl"


class AsyncCallWorker(QObject):
    """Run one network operation away from the Qt GUI thread."""
    result = Signal(object)
    error = Signal(str)
    done = Signal()

    def __init__(self, call):
        super().__init__(); self.call = call

    def run(self):
        try:
            self.result.emit(self.call())
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.done.emit()


class AsyncResultProxy(QObject):
    """Marshal worker signals back to the GUI thread before callbacks run."""
    result = Signal(object)
    error = Signal(str)
    done = Signal()

    @Slot(object)
    def deliver_result(self, value):
        self.result.emit(value)

    @Slot(str)
    def deliver_error(self, message):
        self.error.emit(message)

    @Slot()
    def deliver_done(self):
        self.done.emit()


class WeFishWindow(QMainWindow):
    syncChanged = Signal(str)
    globalAction = Signal(str)
    def __init__(self, client: WeReadClient):
        super().__init__(); self.client = client; self.mock = bool(client.base_url)
        cache_name = "mock-cache" if self.mock else "cache"
        self.cache = LocalCache(Path.home() / ".wefish" / cache_name); self.offline = False
        self.setWindowTitle("上班墨鱼 · WeFish"); self.resize(980, 680); self.setMinimumSize(320, 220); self.normal_geometry = self.geometry(); self.mini_mode = False; self.read_elapsed = 0
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet("""
        QWidget { background:#10151d; color:#e8eef7; font-family:-apple-system,'Segoe UI',sans-serif; }
        QFrame#bar { background:#18202b; border-bottom:1px solid #2a3645; }
        QLabel#brand { font-size:19px; font-weight:700; color:#70d6c2; }
        QLabel#muted { color:#8b9aae; }
        QListWidget, QTextBrowser { background:#121922; border:1px solid #2a3645; border-radius:8px; padding:5px; }
        QListWidget::item { padding:10px; border-radius:6px; }
        QListWidget::item:selected { background:#24463f; }
        QPushButton { background:#243141; border:0; border-radius:6px; padding:8px 12px; }
        QPushButton:hover { background:#315044; }
        QSlider::groove:horizontal { height:4px; background:#334354; }
        QSlider::handle:horizontal { width:14px; margin:-5px 0; border-radius:7px; background:#70d6c2; }
        """)
        self._async_threads = set(); self._async_workers = set(); self._shelf_request_active = False; self._report_request_active = False
        self.office_title_mode = False
        self._quit_requested = False
        self.chapter_sequence = []
        self.chapter_meta_by_uid = {}
        self.article_sequence = []
        self.article_meta_by_id = {}
        self._build_ui(); self._build_tray(); self._build_shortcuts(); self.globalAction.connect(self._handle_global_action); self._update_auth_status(); self.read_timer = QTimer(self); self.read_timer.timeout.connect(self._reading_tick); self.read_timer.start(1000); self.load_shelf()

    def _start_async(self, call, on_result, on_error=None, on_done=None):
        """Run a client call in a short-lived QThread and keep it alive safely."""
        thread = QThread(self)
        worker = AsyncCallWorker(call)
        proxy = AsyncResultProxy(self)
        worker.moveToThread(thread)
        self._async_workers.add(worker)
        thread.started.connect(worker.run)
        worker.result.connect(proxy.deliver_result)
        proxy.result.connect(on_result)
        if on_error:
            worker.error.connect(proxy.deliver_error)
            proxy.error.connect(on_error)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        if on_done:
            worker.done.connect(proxy.deliver_done)
            proxy.done.connect(on_done)

        self._async_threads.add(thread)

        def cleanup():
            self._async_threads.discard(thread)
            self._async_workers.discard(worker)
            proxy.deleteLater()
            thread.deleteLater()

        thread.finished.connect(cleanup)
        thread.start()
        return thread

    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root); outer = QVBoxLayout(root); outer.setContentsMargins(12, 10, 12, 12); outer.setSpacing(10)
        bar = QFrame(); bar.setObjectName("bar"); row = QHBoxLayout(bar); row.setContentsMargins(12, 8, 12, 8)
        self.brand_label = QLabel("◒  上班墨鱼"); self.brand_label.setObjectName("brand"); row.addWidget(self.brand_label)
        self.mode_label = QLabel("WeFish  ·  compact reading"); self.mode_label.setObjectName("muted"); row.addWidget(self.mode_label); row.addStretch()
        self.sync_label = QLabel("未同步"); self.sync_label.setObjectName("muted"); row.addWidget(self.sync_label)
        self.sync_button = QPushButton("同步书架"); self.sync_button.clicked.connect(self.load_shelf); row.addWidget(self.sync_button)
        self.auth_button = QPushButton("授权"); self.auth_button.clicked.connect(self.configure_auth); row.addWidget(self.auth_button)
        self.qr_button = QPushButton("微信扫码"); self.qr_button.clicked.connect(self.start_qr_login); row.addWidget(self.qr_button)
        self.reader_link_button = QPushButton("打开读书链接"); self.reader_link_button.clicked.connect(self.open_reader_link); row.addWidget(self.reader_link_button)
        self.stats_button = QPushButton("统计"); self.stats_button.clicked.connect(self.show_stats); row.addWidget(self.stats_button)
        self.compact_button = QPushButton("小窗模式"); self.compact_button.clicked.connect(self.toggle_compact); row.addWidget(self.compact_button)
        outer.addWidget(bar)
        body = QHBoxLayout(); body.setSpacing(10); outer.addLayout(body, 1)
        left = QFrame(); left_layout = QVBoxLayout(left); left_layout.setContentsMargins(0, 0, 0, 0)
        shelf_title = QLabel("我的书架"); shelf_title.setStyleSheet("font-size:15px;font-weight:700;"); left_layout.addWidget(shelf_title)
        self.shelf = QListWidget(); self.shelf.currentItemChanged.connect(lambda current, _previous: self.open_book(current) if current else None); left_layout.addWidget(self.shelf, 1); body.addWidget(left, 1)
        right = QFrame(); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(0, 0, 0, 0)
        self.book_title = QLabel("选择一本书开始阅读"); self.book_title.setStyleSheet("font-size:20px;font-weight:700;"); right_layout.addWidget(self.book_title)
        self.book_meta = QLabel("书架内容将在这里显示"); self.book_meta.setObjectName("muted"); right_layout.addWidget(self.book_meta)
        self.reader = QTextBrowser(); self.reader.setOpenExternalLinks(False); self.reader.setFont(QFont("serif", 15)); self.reader.verticalScrollBar().valueChanged.connect(self._reader_scroll_changed); right_layout.addWidget(self.reader, 1)
        controls = QHBoxLayout(); self.prev_button = QPushButton("‹ 上一章"); self.next_button = QPushButton("下一章 ›"); self.thought_button = QPushButton("书友想法"); self.notes_button = QPushButton("摘录/笔记"); self.progress = QSlider(Qt.Orientation.Horizontal); self.progress.setRange(0, 100); self.progress.setValue(12)
        self.prev_button.clicked.connect(lambda: self.change_chapter(-1)); self.next_button.clicked.connect(lambda: self.change_chapter(1)); self.thought_button.clicked.connect(self.show_thoughts); self.notes_button.clicked.connect(self.show_notes); self.progress.sliderReleased.connect(self.upload_progress)
        controls.addWidget(self.prev_button); controls.addWidget(self.next_button); controls.addWidget(self.thought_button); controls.addWidget(self.notes_button); controls.addWidget(QLabel("进度")); controls.addWidget(self.progress, 1); right_layout.addLayout(controls); body.addWidget(right, 3)

    def _update_auth_status(self):
        has_web = any(cookie.name in {"wr_skey", "wr_gid"} and cookie.value for cookie in self.client.session.cookies)
        has_gateway = bool(self.client.session.api_key)
        name = str(self.client.session.user_info.get("name") or "")
        account = f"：{name}" if name else ""
        if has_web and has_gateway:
            text, tip = f"已登录{account}", "正文 Cookie 与书架 API Key 均已配置"
        elif has_web:
            text, tip = f"已登录{account} · 正文可用", "已配置 Web Reader Cookie；未配置 Skill API Key，书架同步不可用"
        elif has_gateway:
            text, tip = "书架已授权 · 正文需扫码", "已配置 Skill API Key；正文仍需要 Web Reader Cookie"
        else:
            text, tip = "未授权", "请使用微信扫码，或配置 WeRead API Key"
        self.sync_label.setText(text)
        self.sync_label.setToolTip(tip)

    def _build_tray(self):
        pix = QPixmap(32, 32); pix.fill(Qt.GlobalColor.transparent); painter = QPainter(pix); painter.setBrush(QColor("#70d6c2")); painter.setPen(Qt.PenStyle.NoPen); painter.drawEllipse(3, 3, 26, 26); painter.setBrush(QColor("#18202b")); painter.drawEllipse(10, 10, 12, 12); painter.end()
        self.tray = QSystemTrayIcon(QIcon(pix), self); self.tray.setToolTip("上班墨鱼 WeFish")
        menu = QMenu(self)
        menu.addAction(QAction("显示/隐藏阅读窗", self, triggered=self.toggle_visible))
        menu.addAction(QAction("同步书架", self, triggered=self.load_shelf))
        menu.addAction(QAction("上一章", self, triggered=lambda: self.change_chapter(-1)))
        menu.addAction(QAction("下一章", self, triggered=lambda: self.change_chapter(1)))
        menu.addAction(QAction("微信扫码", self, triggered=self.start_qr_login))
        menu.addAction(QAction("小窗模式", self, triggered=self.toggle_compact))
        self.office_title_action = QAction("工作标题模式", self, triggered=self.toggle_office_title)
        menu.addAction(self.office_title_action)
        menu.addAction(QAction("关于 WeFish", self, triggered=self.show_about))
        menu.addSeparator(); menu.addAction(QAction("退出", self, triggered=self.quit_app))
        self.tray.setContextMenu(menu); self.tray.activated.connect(lambda reason: self.toggle_visible() if reason == QSystemTrayIcon.ActivationReason.Trigger else None); self.tray.show()

    def _build_shortcuts(self):
        # Qt shortcuts work across the reader while it is active. The tray menu
        # remains available when another application owns focus.
        macos = sys.platform == "darwin"
        modifier = shortcut_modifier_for()
        self.shortcut_modifier = modifier
        self.hide_shortcut = QShortcut(QKeySequence(f"{modifier}+Alt+M"), self); self.hide_shortcut.activated.connect(self.toggle_visible)
        self.sync_shortcut = QShortcut(QKeySequence(f"{modifier}+Alt+R"), self); self.sync_shortcut.activated.connect(self.load_shelf)
        self.previous_shortcut = QShortcut(QKeySequence(f"{modifier}+Alt+Left"), self); self.previous_shortcut.activated.connect(lambda: self.change_chapter(-1))
        self.next_shortcut = QShortcut(QKeySequence(f"{modifier}+Alt+Right"), self); self.next_shortcut.activated.connect(lambda: self.change_chapter(1))
        self.global_hotkeys = None
        try:
            if macos:
                # pynput starts its listener on another thread. Probe the
                # accessibility symbol first so a broken/old PyObjC install
                # degrades to tray and in-window shortcuts without an
                # unhandled listener-thread exception.
                from HIServices import AXIsProcessTrusted
                AXIsProcessTrusted()
            from pynput import keyboard
            global_modifier = "<cmd>" if macos else "<ctrl>"
            mapping = {
                # pynput invokes callbacks on its listener thread. Emit a Qt
                # signal so all window mutations happen on the GUI thread.
                f"{global_modifier}+<alt>+m": lambda: self.globalAction.emit("toggle"),
                f"{global_modifier}+<alt>+r": lambda: self.globalAction.emit("sync"),
                f"{global_modifier}+<alt>+<left>": lambda: self.globalAction.emit("previous"),
                f"{global_modifier}+<alt>+<right>": lambda: self.globalAction.emit("next"),
            }
            self.global_hotkeys = keyboard.GlobalHotKeys(mapping); self.global_hotkeys.start()
        except Exception:
            # Accessibility permission or optional dependency may be absent.
            self.global_hotkeys = None

    def _handle_global_action(self, action):
        if action == "toggle":
            self.toggle_visible()
        elif action == "sync":
            self.load_shelf()
        elif action == "previous":
            self.change_chapter(-1)
        elif action == "next":
            self.change_chapter(1)

    def configure_auth(self):
        dialog = QDialog(self); dialog.setWindowTitle("WeRead 授权"); dialog.setModal(True)
        form = QFormLayout(dialog)
        key_edit = QLineEdit(os.environ.get("WEREAD_API_KEY", "")); key_edit.setEchoMode(QLineEdit.EchoMode.Password); key_edit.setPlaceholderText("可选：wrk- 开头的 Skill API Key")
        cookie_edit = QLineEdit(os.environ.get("WEREAD_COOKIE", "")); cookie_edit.setEchoMode(QLineEdit.EchoMode.Password); cookie_edit.setPlaceholderText("可选：wr_vid=…; wr_skey=…")
        form.addRow("Skill API Key", key_edit); form.addRow("Web Reader Cookie", cookie_edit)
        hint = QLabel("两项可只填一项：API Key 用于书架，Cookie 用于正文。凭据保存到系统凭据库。")
        hint.setWordWrap(True); hint.setObjectName("muted"); form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        dialog.resize(520, 160)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        key = key_edit.text().strip(); cookie = cookie_edit.text().strip()
        if not key and not cookie:
            self.show_error("请至少填写 API Key 或 Web Reader Cookie")
            return
        if key:
            self.client.session.api_key = key
        if cookie:
            self.client.set_cookie_string(cookie)
        if not self.client.persist_credentials():
            self.show_error("凭据已载入当前窗口，但系统凭据库保存失败；请检查钥匙串/凭据管理器权限")
        self._update_auth_status()
        if key:
            self.load_shelf()

    def open_reader_link(self):
        reader_url, ok = QInputDialog.getText(
            self,
            "打开微信读书正文",
            "粘贴微信读书的阅读链接（正文使用扫码得到的 Web Reader Cookie）：",
            QLineEdit.EchoMode.Normal,
            "https://weread.qq.com/web/reader/",
        )
        if not ok or not reader_url.strip():
            return
        try:
            book = self.client.import_reader_book(reader_url.strip())
            item = QListWidgetItem(f"{book.get('title', '未命名')}\n{book.get('author', '')}")
            item.setData(Qt.ItemDataRole.UserRole, book)
            self.shelf.addItem(item)
            self.shelf.setCurrentItem(item)
            self.sync_label.setText("已打开阅读链接")
        except (AuthError, WeFishError) as exc:
            self.show_error(str(exc))

    def start_qr_login(self):
        dialog = QDialog(self); dialog.setWindowTitle("微信扫码登录 WeFish"); dialog.setModal(False)
        layout = QVBoxLayout(dialog); image_label = QLabel("正在获取二维码…"); image_label.setAlignment(Qt.AlignmentFlag.AlignCenter); image_label.setMinimumSize(320, 320); layout.addWidget(image_label)
        status = QLabel("正在连接微信读书…"); status.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(status)
        otp_input = QLineEdit(); otp_input.setPlaceholderText("手机若显示四位验证码，请在此输入"); otp_input.setMaxLength(4); otp_input.setClearButtonEnabled(True); layout.addWidget(otp_input)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel); retry_button = buttons.addButton("重新获取", QDialogButtonBox.ButtonRole.ActionRole); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons); dialog.resize(360, 410)
        timer = QTimer(dialog); attempts = {"n": 0, "running": False}; state = {"uid": ""}

        def finished_account(account):
            dialog.accept()
            self._update_auth_status()
            if not account.get("persisted", True):
                QMessageBox.warning(
                    self,
                    "登录已完成但未保存",
                    "本次扫码会话已建立，但系统凭据库没有保存成功。\n\n"
                    "当前窗口仍可继续阅读；请保持应用运行，或检查系统钥匙串/凭据管理器权限。",
                )
            if account.get("needs_skill"):
                answer = QMessageBox.warning(
                    self,
                    "需要开启微信读书 Skill",
                    "扫码已成功，但微信读书没有返回官方 API Key。\n\n"
                    "正文读取仍会使用扫码得到的 Web Reader Cookie；若要同步书架，"
                    "请在微信读书 App 进入：我 → 设置 → 微信读书 Skill → 获取 API Key，"
                    "然后点击“授权”粘贴 API Key。",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Open,
                )
                if answer == QMessageBox.StandardButton.Open:
                    QDesktopServices.openUrl(QUrl("https://weread.qq.com/r/weread-skills"))
            else:
                self.load_shelf()

        def handle_result(payload):
            if not dialog.isVisible():
                return
            attempts["running"] = False
            login = payload.get("login") or {}
            if login.get("succeed"):
                status.setText("扫码成功，正在完成授权…")
                finished_account(payload.get("account") or {})
                return
            if login.get("expired") or login.get("logicCode") in {"LOGIN_TIMEOUT", "OTP_EXPIRED"} or attempts["n"] >= 150:
                timer.stop(); status.setText("二维码已过期，请点击“重新获取”"); return
            if login.get("logicCode") == "NEED_OTP":
                status.setText("请输入手机显示的四位验证码，然后等待确认…")
            elif login.get("logicCode") == "OTP_NOT_MATCH":
                status.setText("验证码不正确，请重新输入四位验证码")
            else:
                status.setText("等待扫码确认…")

        def handle_error(message):
            if not dialog.isVisible():
                return
            attempts["running"] = False
            timer.stop(); status.setText(message)

        def handle_begin(uid):
            if not dialog.isVisible():
                return
            state["uid"] = str(uid or "")
            try:
                import qrcode
                image = qrcode.make(f"https://weread.qq.com/web/confirm?uid={state['uid']}")
                stream = io.BytesIO(); image.save(stream, format="PNG")
                pixmap = QPixmap(); pixmap.loadFromData(stream.getvalue(), "PNG")
                image_label.setText(""); image_label.setPixmap(pixmap.scaled(320, 320, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                status.setText("请使用微信读书 App 扫码确认")
                timer.start(2000)
            except Exception as exc:
                status.setText(f"无法生成登录二维码：{exc}")

        def begin_round():
            if not dialog.isVisible() or attempts["running"]:
                return
            timer.stop()
            attempts["n"] = 0
            state["uid"] = ""
            image_label.setPixmap(QPixmap())
            image_label.setText("正在获取二维码…")
            status.setText("正在连接微信读书…")
            self._start_async(self.client.qr_begin, handle_begin, handle_error)

        def poll_async():
            if attempts["running"] or not dialog.isVisible() or not state["uid"]: return
            attempts["n"] += 1
            if attempts["n"] > 150:
                timer.stop(); status.setText("二维码已过期，请点击“重新获取”"); return
            attempts["running"] = True
            def poll_and_complete():
                login = self.client.qr_poll(state["uid"], otp=otp_input.text().strip())
                if login.get("succeed"):
                    return {"login": login, "account": self.client.qr_complete(login)}
                return {"login": login}
            self._start_async(poll_and_complete, handle_result, handle_error)

        dialog.finished.connect(lambda _code: timer.stop())
        timer.timeout.connect(poll_async); retry_button.clicked.connect(begin_round); dialog.show()
        self.qr_dialog = dialog
        begin_round()

    @staticmethod
    def _shelf_books(data):
        """Normalize regular books plus the optional public-account shelf item."""
        if not isinstance(data, dict):
            return []
        books = [row for row in data.get("books", []) if isinstance(row, dict)]
        raw_mp = data.get("mp")
        candidates = raw_mp if isinstance(raw_mp, list) else ([raw_mp] if isinstance(raw_mp, dict) else [])
        known = {str(row.get("bookId") or "") for row in books}
        for row in candidates:
            info = row.get("bookInfo") if isinstance(row.get("bookInfo"), dict) else row
            book_id = str(info.get("bookId") or info.get("book_id") or "")
            if not book_id or book_id in known:
                continue
            item = dict(info)
            item["bookId"] = book_id
            item["isMp"] = str(book_id).startswith("MP_WXS_")
            item.setdefault("author", "公众号")
            books.append(item)
            known.add(book_id)
        return books

    def _add_shelf_books(self, data):
        books = self._shelf_books(data)
        self.shelf.clear()
        for book in books:
            label = f"{book.get('title', '未命名')}\n{book.get('author', '')}"
            if book.get("isMp") or str(book.get("bookId", "")).startswith("MP_WXS_"):
                label += "  ·  公众号"
            item = QListWidgetItem(label); item.setData(Qt.ItemDataRole.UserRole, book); self.shelf.addItem(item)
        return books

    def load_shelf(self):
        if self._shelf_request_active:
            return
        self._shelf_request_active = True
        self.sync_button.setEnabled(False)
        self.sync_label.setText("同步中…")

        def loaded(data):
            self.cache.put("shelf", data); self.offline = False
            books = self._add_shelf_books(data)
            self.sync_label.setText(f"已同步 {len(books)} 本"); self.syncChanged.emit(self.sync_label.text())

        def failed(message):
            data = self.cache.get("shelf")
            if data:
                books = self._add_shelf_books(data)
                self.sync_label.setText(f"离线缓存 {len(books)} 本"); self.offline = True
            elif "API key is not configured" in str(message):
                # A fresh installation is expected to start without the
                # optional Skill key. Keep the main window usable so the user
                # can scan or paste a reader link instead of dismissing a
                # blocking error dialog before seeing the login controls.
                self._update_auth_status()
            else:
                self.sync_label.setText("需要授权")
                if self.isVisible(): self.show_error(message)

        def done():
            self._shelf_request_active = False
            self.sync_button.setEnabled(True)

        self._start_async(self.client.shelf, loaded, failed, done)

    def _prepare_book(self, book, catalog):
        self.chapter_sequence = []
        self.chapter_meta_by_uid = {}
        self.article_sequence = []
        self.article_meta_by_id = {}
        entitlements = self.client.entitlement_summary(catalog)
        book["entitlements"] = entitlements
        meta = f"{book.get('author', '')}  ·  {book.get('category', '书籍')}"
        if entitlements.get("has_paid_content"):
            meta += f"  ·  服务端标记 {entitlements.get('paid_chapters', 0)} 个付费章节"
        self.book_meta.setText(meta)
        try:
            readable = [x for x in catalog if int(x.get("wordCount", 1) or 0) > 0 and x.get("title") != "封面"]
            self.chapter_sequence = [int(x.get("chapterUid")) for x in readable if x.get("chapterUid") is not None]
            self.chapter_meta_by_uid = {int(x.get("chapterUid")): x for x in readable if x.get("chapterUid") is not None}
        except (TypeError, ValueError):
            self.chapter_sequence = []
            self.chapter_meta_by_uid = {}
        preferred = int(book.get("chapterUid", 0) or 0)
        first = preferred if preferred in self.chapter_sequence else (self.chapter_sequence[0] if self.chapter_sequence else max(1, preferred))
        self.render_chapter(first)

    def _prepare_mp_book(self, book, articles):
        self.chapter_sequence = []
        self.chapter_meta_by_uid = {}
        self.article_sequence = []
        self.article_meta_by_id = {}
        for row in articles if isinstance(articles, list) else []:
            if not isinstance(row, dict) or not row.get("reviewId"):
                continue
            article_id = str(row["reviewId"])
            self.article_sequence.append(article_id)
            self.article_meta_by_id[article_id] = row
        if not self.article_sequence:
            self._set_reader_message("公众号没有可读文章")
            return
        preferred = str(book.get("chapterUid") or "")
        first = preferred if preferred in self.article_sequence else self.article_sequence[0]
        self.render_article(first)

    def _book_catalog_failed(self, message):
        self.chapter_sequence = []
        self.chapter_meta_by_uid = {}
        try:
            chapter = max(1, int(getattr(self, "current_chapter", 1) or 1))
        except (TypeError, ValueError):
            chapter = 1
        self.render_chapter(chapter, error_message=message)

    def _mp_catalog_failed(self, message):
        self.article_sequence = []
        self.article_meta_by_id = {}
        self.render_article(getattr(self, "current_article_id", ""), error_message=message)

    def _set_reader_message(self, message):
        self.reader.setHtml(f"<p>{html.escape(str(message))}</p>")

    def open_book(self, item):
        book = item.data(Qt.ItemDataRole.UserRole) or {}
        self.current_book = book
        self.book_title.setText(book.get("title", "未命名"))
        is_mp = bool(book.get("isMp") or self.client.is_mp_book(book.get("bookId", "")))
        self.book_meta.setText(f"{book.get('author', '')}  ·  {'公众号' if is_mp else book.get('category', '书籍')}")
        self.progress.setValue(int(book.get("progress", 12) or 12))
        self.current_chapter = book.get("chapterUid", 1) or 1
        self.current_article_id = ""
        self.chapter_sequence = []
        self.chapter_meta_by_uid = {}
        self.article_sequence = []
        self.article_meta_by_id = {}
        if is_mp:
            self._set_reader_message("正在加载公众号文章…")
            articles = book.get("articles")
            if articles:
                self._prepare_mp_book(book, articles)
                return
            book_id = str(book.get("bookId", ""))

            def articles_loaded(rows):
                if getattr(self, "current_book", {}).get("bookId") != book_id:
                    return
                book["articles"] = rows
                self._prepare_mp_book(book, rows)

            self._start_async(
                lambda: self.client.mp_articles(book_id),
                articles_loaded,
                self._mp_catalog_failed,
            )
            return

        self._set_reader_message("正在加载章节目录…")
        catalog = book.get("chapters")
        if catalog:
            self._prepare_book(book, catalog)
            return
        book_id = str(book.get("bookId", ""))

        def catalog_loaded(rows):
            if getattr(self, "current_book", {}).get("bookId") != book_id:
                return
            self._prepare_book(book, rows)

        def catalog_failed(message):
            if getattr(self, "current_book", {}).get("bookId") != book_id:
                return
            self._book_catalog_failed(message)

        self._start_async(
            lambda: self.client.chapter_catalog(book_id),
            catalog_loaded,
            catalog_failed,
        )

    def _reading_tick(self):
        if hasattr(self, "current_book") and self.isVisible():
            self.read_elapsed += 1
            if self.article_sequence and getattr(self, "current_article_id", ""):
                # The Web Reader MP article endpoint has no chapterUid-based
                # progress payload. Keep local elapsed time visible and wait
                # for the article-specific reporting contract before sending
                # an invalid regular-book payload.
                return
            if self.read_elapsed >= 60 and not self._report_request_active:
                self._report_request_active = True
                elapsed = self.read_elapsed

                def reported(_result):
                    self.read_elapsed = 0

                def report_failed(_message):
                    self.sync_label.setText("进度待联网补报")

                def report_done():
                    self._report_request_active = False

                payload = {"bookId": self.current_book.get("bookId", ""), "chapterUid": getattr(self, "current_chapter", 1), "chapterIdx": self._current_chapter_idx(), "chapterOffset": getattr(self, "current_chapter_offset", 0), "summary": self._reader_summary(), "progress": self.progress.value(), "elapsed": elapsed}
                self._start_async(
                    lambda: self.client.report_read(payload),
                    reported,
                    report_failed,
                    report_done,
                )

    def render_chapter(self, chapter, error_message=None):
        self.current_chapter = int(chapter or 1)
        self.current_article_id = ""
        self.current_chapter_offset = 0
        if error_message:
            cached = self.cache.get(f"chapter-{self.current_book.get('bookId', 'unknown')}-{self.current_chapter}")
            if cached:
                self.offline = True; self.reader.setHtml(cached.get("html", ""))
            else:
                self._set_reader_message(error_message)
            return
        book_id = self.current_book.get("bookId", "")
        chapter = self.current_chapter
        self._set_reader_message(f"正在加载第 {chapter} 章…")

        def loaded(source):
            if getattr(self, "current_book", {}).get("bookId") != book_id or getattr(self, "current_chapter", None) != chapter:
                return
            self.cache.put(f"chapter-{book_id}-{chapter}", {"html": source}); self.offline = False
            self._reader_loading = True
            self.reader.setHtml(source)
            self._reader_loading = False

        def failed(message):
            if getattr(self, "current_book", {}).get("bookId") != book_id or getattr(self, "current_chapter", None) != chapter:
                return
            cached = self.cache.get(f"chapter-{book_id}-{chapter}")
            if cached:
                self.offline = True; self.reader.setHtml(cached.get("html", ""))
            else:
                self.reader.setHtml(
                    f"<h2>第{chapter}章</h2>"
                    f"<p>正文暂时不可用：{html.escape(str(message))}</p>"
                    "<p>微信读书 API Key 只负责书架、统计和笔记接口；正文需要 Web Reader Cookie。"
                    "请点击“微信扫码”完成登录后再试。</p>"
                )

        self._start_async(lambda: self.client.fetch_chapter(book_id, chapter), loaded, failed)

    def render_article(self, article_id, error_message=None):
        article_id = str(article_id or "")
        self.current_article_id = article_id
        self.current_chapter = article_id
        self.current_chapter_offset = 0
        article = getattr(self, "article_meta_by_id", {}).get(article_id, {})
        title = str(article.get("title") or "公众号文章")
        cache_key = f"article-{self.current_book.get('bookId', 'unknown')}-{article_id}"
        if error_message:
            cached = self.cache.get(cache_key)
            if cached:
                self.offline = True; self.reader.setHtml(cached.get("html", ""))
            else:
                self._set_reader_message(error_message)
            return
        book_id = str(self.current_book.get("bookId", ""))
        self._set_reader_message(f"正在加载《{title}》…")

        def loaded(source):
            if getattr(self, "current_book", {}).get("bookId") != book_id or getattr(self, "current_article_id", "") != article_id:
                return
            self.cache.put(cache_key, {"html": source}); self.offline = False
            self._reader_loading = True; self.reader.setHtml(source); self._reader_loading = False

        def failed(message):
            if getattr(self, "current_book", {}).get("bookId") != book_id or getattr(self, "current_article_id", "") != article_id:
                return
            cached = self.cache.get(cache_key)
            if cached:
                self.offline = True; self.reader.setHtml(cached.get("html", ""))
            else:
                self.reader.setHtml(f"<h2>{html.escape(title)}</h2><p>正文暂时不可用：{html.escape(str(message))}</p>")

        self._start_async(lambda: self.client.mp_content(article_id), loaded, failed)

    def change_chapter(self, delta):
        if not hasattr(self, "current_book"): return
        current_article = getattr(self, "current_article_id", "")
        if self.article_sequence and current_article in self.article_sequence:
            index = max(0, min(len(self.article_sequence) - 1, self.article_sequence.index(current_article) + delta))
            self.render_article(self.article_sequence[index])
            return
        current = getattr(self, "current_chapter", 1)
        if self.chapter_sequence and current in self.chapter_sequence:
            index = max(0, min(len(self.chapter_sequence) - 1, self.chapter_sequence.index(current) + delta))
            self.render_chapter(self.chapter_sequence[index])
        else:
            self.render_chapter(max(1, current + delta))

    def _current_chapter_idx(self):
        if self.article_sequence and getattr(self, "current_article_id", "") in self.article_sequence:
            return self.article_sequence.index(self.current_article_id)
        row = getattr(self, "chapter_meta_by_uid", {}).get(getattr(self, "current_chapter", 1), {})
        return int(row.get("chapterIdx", getattr(self, "current_chapter", 1)) or 0)

    def _reader_summary(self):
        text = " ".join(self.reader.toPlainText().split())
        offset = max(0, int(getattr(self, "current_chapter_offset", 0) or 0))
        return text[offset:offset + 20]

    def _reader_scroll_changed(self, value):
        if getattr(self, "_reader_loading", False) or not hasattr(self, "current_book"):
            return
        bar = self.reader.verticalScrollBar()
        maximum = max(0, int(bar.maximum()))
        fraction = (float(value) / maximum) if maximum else 0.0
        try:
            top = int(self.reader.cursorForPosition(QPoint(6, 6)).position())
            bottom = int(self.reader.cursorForPosition(QPoint(6, max(6, self.reader.viewport().height() - 6))).position())
            self.current_chapter_offset = max(0, top, bottom)
        except (AttributeError, RuntimeError):
            self.current_chapter_offset = max(0, int(value))
        if self.current_chapter_offset == 0 and value:
            # QTextBrowser can return a zero cursor while its document layout
            # is still settling; retain a monotonic, nonzero position sample.
            self.current_chapter_offset = int(value)
        sequence = getattr(self, "chapter_sequence", [])
        current = getattr(self, "current_chapter", None)
        if not sequence:
            sequence = getattr(self, "article_sequence", [])
            current = getattr(self, "current_article_id", "")
        if sequence and current in sequence:
            index = sequence.index(current)
            percent = int(max(0.0, min(100.0, ((index + fraction) / len(sequence)) * 100)))
            self.progress.blockSignals(True)
            self.progress.setValue(percent)
            self.progress.blockSignals(False)

    def upload_progress(self):
        if not hasattr(self, "current_book") or self._report_request_active: return
        if self.article_sequence and getattr(self, "current_article_id", ""):
            self.sync_label.setText("公众号文章进度由服务端记录")
            return
        self._report_request_active = True
        elapsed = self.read_elapsed

        def reported(_result):
            self.read_elapsed = 0; self.sync_label.setText("进度已上报")

        def failed(_message):
            self.sync_label.setText("进度待联网补报")

        def done():
            self._report_request_active = False

        payload = {"bookId": self.current_book.get("bookId", ""), "chapterUid": getattr(self, "current_chapter", 1), "chapterIdx": self._current_chapter_idx(), "chapterOffset": getattr(self, "current_chapter_offset", 0), "summary": self._reader_summary(), "progress": self.progress.value(), "elapsed": elapsed}
        self._start_async(
            lambda: self.client.report_read(payload),
            reported,
            failed,
            done,
        )

    def show_thoughts(self):
        if not hasattr(self, "current_book"): return
        if self.article_sequence and getattr(self, "current_article_id", ""):
            self.show_error("公众号文章暂不提供章节书友想法接口")
            return
        def loaded(data):
            reviews = data.get("reviews", [])
            mine = data.get("my_reviews", [])
            rows = [f"<p><b>书友：</b>{html.escape(str(x.get('content', '')))}</p>" for x in reviews]
            rows += [f"<p><b>我：</b>{html.escape(str(x.get('content', '')))}</p>" for x in mine]
            text = "<h3>书友想法</h3>" + ("".join(rows) if rows else "<p>本章暂无想法</p>")
            QMessageBox.information(self, "书友想法", text)

        self._start_async(
            lambda: self.client.annotations(self.current_book.get("bookId", "wf-001"), getattr(self, "current_chapter", 1)),
            loaded,
            self.show_error,
        )

    def show_notes(self):
        if not hasattr(self, "current_book"): return
        if self.article_sequence and getattr(self, "current_article_id", ""):
            self.show_error("公众号文章暂不提供章节摘录接口")
            return

        def loaded(data):
            underlines = data.get("underlines", [])
            rows = []
            for row in underlines:
                text = row.get("markText", row.get("content", row.get("text", "")))
                if text:
                    rows.append(f"<p><b>摘录：</b>“{html.escape(str(text))}”</p>")
            for row in data.get("my_reviews", []):
                text = row.get("content", row.get("text", ""))
                if text:
                    rows.append(f"<p><b>我的想法：</b>{html.escape(str(text))}</p>")
            body = "".join(rows) or "<p>本章暂无摘录或笔记</p>"
            QMessageBox.information(self, "摘录与笔记", "<h3>本章摘录与笔记</h3>" + body)

        self._start_async(
            lambda: self.client.annotations(self.current_book.get("bookId", "wf-001"), getattr(self, "current_chapter", 1)),
            loaded,
            self.show_error,
        )

    def show_stats(self):
        def loaded(result):
            stats, notes = result
            seconds = int(stats.get("readingTime", 0) or 0)
            hours, minutes = divmod(seconds // 60, 60)
            text = (f"<h3>本月阅读统计</h3><p>阅读时长：{hours} 小时 {minutes} 分钟</p>"
                    f"<p>阅读天数：{stats.get('readingDays', '未知')}</p>"
                    f"<p>笔记总数：{notes.get('totalNoteCount', '未知')}</p>"
                    "<p>会员与付费权益：由微信读书账号服务端控制，WeFish 不绕过或伪造。</p>")
            QMessageBox.information(self, "阅读统计", text)

        self._start_async(
            lambda: (self.client.read_stats("monthly"), self.client.notebooks()),
            loaded,
            self.show_error,
        )

    def show_about(self):
        QMessageBox.information(
            self,
            "关于上班墨鱼",
            "上班墨鱼 · WeFish\n\n"
            "本程序按 GNU Affero General Public License 3.0 发布。\n"
            "正文代理与协议研究参考 finlater/weread.koplugin；\n"
            "WeFish 不隶属于微信读书、腾讯或 KOReader。\n\n"
            "源码与许可证见项目目录中的 README.md、NOTICE 和 LICENSE。",
        )

    def quit_app(self):
        """Exit from the tray even though the normal close button hides."""
        self._quit_requested = True
        self.tray.hide()
        self.read_timer.stop()
        self._stop_async_threads()
        QApplication.quit()

    def toggle_visible(self): self.hide() if self.isVisible() else (self.show(), self.raise_(), self.activateWindow())

    def toggle_office_title(self):
        self.office_title_mode = not self.office_title_mode
        if self.office_title_mode:
            self.setWindowTitle("工作文档")
            self.brand_label.setText("▤  工作文档")
            self.mode_label.setText("阅读窗口")
            self.office_title_action.setText("恢复 WeFish 标题")
        else:
            self.setWindowTitle("上班墨鱼 · WeFish")
            self.brand_label.setText("◒  上班墨鱼")
            self.mode_label.setText("WeFish  ·  compact reading")
            self.office_title_action.setText("工作标题模式")

    def toggle_compact(self):
        if not self.mini_mode:
            self.normal_geometry = self.geometry(); self.mini_mode = True; self.resize(390, 270); self.setWindowOpacity(0.88); self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True); self.compact_button.setText("恢复窗口")
        else:
            self.mini_mode = False; self.setWindowOpacity(1.0); self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False); self.setGeometry(self.normal_geometry); self.compact_button.setText("小窗模式")
        self.show(); self.raise_()
    def show_error(self, message): QMessageBox.warning(self, "WeFish", message)

    def _stop_async_threads(self):
        if self.global_hotkeys is not None:
            try:
                self.global_hotkeys.stop()
            except Exception:
                pass
            self.global_hotkeys = None
        for thread in list(self._async_threads):
            thread.requestInterruption()
            thread.quit()
            thread.wait(2000)

    def closeEvent(self, event):
        if self.tray.isVisible() and not self._quit_requested: self.hide(); event.ignore()
        else:
            self._stop_async_threads()
            event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--mock", action="store_true"); args = parser.parse_args(argv)
    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv]); app.setQuitOnLastWindowClosed(False)
    mock_server = None
    mock_thread = None
    if args.mock:
        from mock_service import serve
        import threading
        mock_server = serve(); mock_thread = threading.Thread(target=mock_server.serve_forever, daemon=True); mock_thread.start()
        client = WeReadClient(base_url=f"http://127.0.0.1:{mock_server.server_port}")
    else:
        client = WeReadClient()
        client.restore_credentials()
        environment_key = os.environ.get("WEREAD_API_KEY", "").strip()
        if environment_key:
            client.session.api_key = environment_key
        environment_cookie = os.environ.get("WEREAD_COOKIE", "")
        if environment_cookie:
            client.set_cookie_string(environment_cookie)
    if args.mock:
        pass
    window = WeFishWindow(client); window.show(); return app.exec()


if __name__ == "__main__": main()

"""Unified single-window UI for ViunaLink 1.1 – Persian UI.

One page / one window that switches between Host and Viewer modes.
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPointF, QRectF
from PyQt6.QtGui import QColor, QPainter, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
    QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from client.input_handler import InputForwarder
from client.viewer import RemoteSession
from core import security
from core.discovery import discover
from core.services import Services, default_download_dir
from server.host import Host
from utils.config import Config
from utils.helpers import local_ips, session_warning
from utils.qt_bridge import ClipboardBridge, Prompter
from PyQt6.QtWidgets import QInputDialog
from core.relay_client import RelaySettings
from ui.settings_dialog import SettingsDialog, ask_settings_access
from utils.addressbook import AddressBook, Entry

_MOUSE = {
    Qt.MouseButton.LeftButton: 1,
    Qt.MouseButton.RightButton: 2,
    Qt.MouseButton.MiddleButton: 3,
}
_ID_RE = re.compile(r"\d{3}\s?\d{3}\s?\d{3}")


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

DARK_QSS = """
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Segoe UI", "Tahoma", "Vazirmatn", "Ubuntu", sans-serif;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: #89b4fa;
}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #cdd6f4;
    selection-background-color: #89b4fa;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
    border: 1px solid #89b4fa;
}
QPushButton {
    background-color: #45475a;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    color: #cdd6f4;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #585b70;
}
QPushButton:pressed {
    background-color: #313244;
}
QPushButton:disabled {
    background-color: #313244;
    color: #6c7086;
}
QPushButton#primary {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: 600;
}
QPushButton#primary:hover {
    background-color: #b4befe;
}
QPushButton#danger {
    background-color: #f38ba8;
    color: #1e1e2e;
}
QPushButton#danger:hover {
    background-color: #eba0ac;
}
QPushButton#modeBtn {
    background-color: transparent;
    border: 1px solid #45475a;
    border-radius: 8px;
    padding: 14px 20px;
    font-size: 14px;
    text-align: right;
}
QPushButton#modeBtn:hover {
    background-color: #313244;
    border-color: #89b4fa;
}
QPushButton#modeBtn[active="true"] {
    background-color: #313244;
    border: 2px solid #89b4fa;
    color: #89b4fa;
}
QLabel#title {
    font-size: 22px;
    font-weight: 700;
    color: #cdd6f4;
}
QLabel#subtitle {
    color: #a6adc8;
    font-size: 12px;
}
QLabel#idLabel {
    font-size: 26px;
    font-weight: 700;
    color: #89b4fa;
    letter-spacing: 2px;
}
QLabel#statusOk {
    color: #a6e3a1;
    font-weight: 600;
}
QLabel#statusWait {
    color: #f9e2af;
    font-weight: 600;
}
QLabel#statusOff {
    color: #6c7086;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #45475a;
    background: #313244;
}
QCheckBox::indicator:checked {
    background: #89b4fa;
    border-color: #89b4fa;
}
QStatusBar {
    background: #181825;
    color: #a6adc8;
    border-top: 1px solid #313244;
}
QPlainTextEdit {
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 12px;
}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#1e1e2e"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#cdd6f4"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#313244"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#1e1e2e"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#cdd6f4"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#cdd6f4"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#cdd6f4"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#45475a"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#cdd6f4"))
    pal.setColor(QPalette.ColorRole.BrightText, QColor("#f38ba8"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#89b4fa"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#1e1e2e"))
    app.setPalette(pal)
    app.setStyleSheet(DARK_QSS)


# ---------------------------------------------------------------------------
# Remote view widget
# ---------------------------------------------------------------------------

class RemoteView(QWidget):
    def __init__(self, forwarder: InputForwarder):
        super().__init__()
        self.fwd = forwarder
        self.image = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(400, 280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, img) -> None:
        self.image = img
        self.update()

    def _target(self) -> QRectF:
        if self.image is None:
            return QRectF()
        iw, ih = self.image.width(), self.image.height()
        s = min(self.width() / iw, self.height() / ih)
        w, h = iw * s, ih * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _norm(self, pos: QPointF):
        r = self._target()
        if r.width() <= 0 or not r.contains(pos):
            return None
        return (pos.x() - r.x()) / r.width(), (pos.y() - r.y()) / r.height()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#11111b"))
        if self.image is not None:
            p.drawImage(self._target(), self.image)
        else:
            p.setPen(QColor("#6c7086"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "در انتظار تصویر…")

    def mouseMoveEvent(self, e):
        n = self._norm(e.position())
        if n:
            self.fwd.move(*n)

    def mousePressEvent(self, e):
        self._button(e, True)

    def mouseReleaseEvent(self, e):
        self._button(e, False)

    def _button(self, e, pressed):
        b = _MOUSE.get(e.button())
        n = self._norm(e.position())
        if b and n:
            self.fwd.move(*n, force=True)
            self.fwd.button(b, pressed)

    def wheelEvent(self, e):
        d = e.angleDelta()
        step = lambda v: 0 if v == 0 else (v // 120 or (1 if v > 0 else -1))
        self.fwd.scroll(step(d.x()), step(d.y()))

    def keyPressEvent(self, e):
        self.fwd.key(e, True)

    def keyReleaseEvent(self, e):
        self.fwd.key(e, False)

    def focusNextPrevChild(self, _next):
        return False

    def focusOutEvent(self, e):
        self.fwd.release_all()
        super().focusOutEvent(e)


# ---------------------------------------------------------------------------
# Host panel
# ---------------------------------------------------------------------------

def _list_monitors() -> list[tuple[int, int, int]]:
    try:
        import mss
        with mss.mss() as s:
            return [(i, m["width"], m["height"]) for i, m in enumerate(s.monitors)]
    except Exception:
        return [(1, 0, 0)]


class HostPanel(QWidget):
    note = pyqtSignal(str)
    peers = pyqtSignal(list)
    started = pyqtSignal(object, object)
    relay_sig = pyqtSignal(str)

    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.host: Optional[Host] = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True,
                         name="remotelink-host").start()
        self.prompter = Prompter(self)
        self.clipboard = ClipboardBridge()
        self.clipboard.enabled = cfg.share_clipboard
        self.services = Services(
            clipboard=self.clipboard, ask=self.prompter.ask_async,
            notify=self.note.emit, sessions_changed=self.peers.emit,
            relay_status=self.relay_sig.emit,
            dest_dir=cfg.dest_dir(), max_file_bytes=cfg.max_file_mb << 20)
        self.note.connect(self._log)
        self.peers.connect(self._set_peers)
        self.started.connect(self._on_started)
        self.relay_sig.connect(self._set_relay)
        self._otp_pw = None
        self._build()
        self._otp_timer = QTimer(self)
        self._otp_timer.timeout.connect(self._tick_otp)
        self._otp_timer.start(1000)
        self.addr.setText("، ".join(f"{ip}:{cfg.port}" for ip in local_ips()) or "—")
        warn = session_warning()
        if warn:
            self._log(warn)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        box = QGroupBox("این کامپیوتر")
        f = QFormLayout(box)
        f.setSpacing(10)
        self.name_label = QLabel(self.cfg.device_name)
        self.name_label.setObjectName("subtitle")
        mid = self.cfg.machine_id
        self.id_label = QLabel(f"{mid[:3]} {mid[3:6]} {mid[6:]}")
        self.id_label.setObjectName("idLabel")
        self.id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pw = QLineEdit(self.cfg.password)
        self.pw.setReadOnly(True)
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        show_pw = QPushButton("نمایش")
        show_pw.setFixedWidth(70)
        show_pw.clicked.connect(self._toggle_pw)
        row = QHBoxLayout()
        row.addWidget(self.pw, 1)
        row.addWidget(show_pw)
        self.otp = QLineEdit()
        self.otp.setReadOnly(True)
        self.otp.setPlaceholderText("پس از شروع اشتراک بسازید")
        self.otp_btn = QPushButton("ساخت رمز یکبارمصرف")
        self.otp_btn.setEnabled(False)
        self.otp_btn.clicked.connect(self._new_otp)
        orow = QHBoxLayout()
        orow.addWidget(self.otp, 1)
        orow.addWidget(self.otp_btn)
        self.relay_lbl = QLabel("")
        self.addr = QLabel("")
        self.addr.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.addr.setObjectName("subtitle")
        f.addRow("نام:", self.name_label)
        f.addRow("شناسه:", self.id_label)
        f.addRow("رمز دائمی:", row)
        f.addRow("رمز یکبارمصرف:", orow)
        f.addRow("اینترنت (ریلی):", self.relay_lbl)
        f.addRow("آدرس شبکه‌ی محلی:", self.addr)
        root.addWidget(box)
        self.refresh_credentials()
        self._set_relay("off" if not self.cfg.relay_server else "offline")

        opt = QGroupBox("نمایش")
        g = QFormLayout(opt)
        g.setSpacing(8)
        self.monitor = QComboBox()
        for i, w, h in _list_monitors():
            self.monitor.addItem("همه مانیتورها" if i == 0 else f"مانیتور {i} ({w}×{h})", i)
        k = self.monitor.findData(self.cfg.monitor)
        self.monitor.setCurrentIndex(max(0, k))
        self.monitor.currentIndexChanged.connect(
            lambda _i: setattr(self.cfg, "monitor", self.monitor.currentData()))
        g.addRow("مانیتور:", self.monitor)
        hint = QLabel("سایر گزینه‌ها (رمزها، دسترسی‌ها، شبکه، ریلی) در «تنظیمات» قفل‌دار هستند.")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        g.addRow(hint)
        root.addWidget(opt)

        self.state = QLabel("متوقف")
        self.state.setObjectName("statusOff")
        self.btn = QPushButton("شروع اشتراک‌گذاری")
        self.btn.setObjectName("primary")
        self.btn.setMinimumHeight(42)
        self.btn.clicked.connect(self._toggle)
        self.kick = QPushButton("قطع اتصال بیننده")
        self.kick.setObjectName("danger")
        self.kick.clicked.connect(self._kick)
        self.sendf = QPushButton("ارسال فایل…")
        self.sendf.clicked.connect(self._send_file)
        self.kick.setEnabled(False)
        self.sendf.setEnabled(False)
        btns = QHBoxLayout()
        btns.addWidget(self.btn, 2)
        btns.addWidget(self.kick, 1)
        btns.addWidget(self.sendf, 1)
        root.addWidget(self.state)
        root.addLayout(btns)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(400)
        self.logbox.setPlaceholderText("گزارش فعالیت…")
        root.addWidget(self.logbox, 1)

    def _spin(self, lo, hi, val, attr) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(val)
        s.valueChanged.connect(lambda v, a=attr: setattr(self.cfg, a, v))
        return s

    def _flag(self, attr: str, on: bool) -> None:
        setattr(self.cfg, attr, on)
        if attr == "share_clipboard":
            self.clipboard.enabled = on

    def _toggle_pw(self) -> None:
        if self.pw.echoMode() == QLineEdit.EchoMode.Password:
            self.pw.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.pw.setEchoMode(QLineEdit.EchoMode.Password)

    def _log(self, msg: str) -> None:
        self.logbox.appendPlainText(msg)

    def _new_password(self) -> None:
        self.cfg.password = security.generate_password()
        self.pw.setText(self.cfg.password)
        self.cfg.save()
        self._log("رمز عبور جدید ساخته شد")

    def _toggle(self) -> None:
        if self.host is None:
            self.btn.setEnabled(False)
            host = Host(self.cfg, services=self.services)
            fut = asyncio.run_coroutine_threadsafe(host.start(), self.loop)
            fut.add_done_callback(
                lambda f, h=host: self.started.emit(h, f.exception()))
        else:
            asyncio.run_coroutine_threadsafe(self.host.stop(), self.loop)
            self.host = None
            self._running(False)
            self._log("اشتراک‌گذاری متوقف شد")

    def _on_started(self, host, exc) -> None:
        self.btn.setEnabled(True)
        if exc is not None:
            QMessageBox.critical(self, "ViunaLink", f"خطا در شروع اشتراک‌گذاری:\n{exc}")
            return
        self.host = host
        self.cfg.save()
        self._running(True)
        self._log(f"اشتراک‌گذاری روی پورت {self.cfg.port} شروع شد")

    def _running(self, on: bool) -> None:
        self.btn.setText("توقف اشتراک‌گذاری" if on else "شروع اشتراک‌گذاری")
        self.btn.setObjectName("danger" if on else "primary")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        self.otp_btn.setEnabled(on)
        self.state.setText("در حال اشتراک — منتظر بیننده" if on else "متوقف")
        self.state.setObjectName("statusWait" if on else "statusOff")
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)
        ips = "، ".join(f"{ip}:{self.cfg.port}" for ip in local_ips())
        self.addr.setText(ips or "—")
        if not on:
            self.kick.setEnabled(False)
            self.sendf.setEnabled(False)
            self.otp.clear()
            self._otp_pw = None

    def _set_peers(self, peers: list) -> None:
        if self.host is None:
            return
        if peers:
            self.state.setText(f"بیننده متصل: {', '.join(peers)}")
            self.state.setObjectName("statusOk")
        else:
            self.state.setText("در حال اشتراک — منتظر بیننده")
            self.state.setObjectName("statusWait")
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)
        self.kick.setEnabled(bool(peers))
        self.sendf.setEnabled(bool(peers))

    def refresh_credentials(self) -> None:
        self.name_label.setText(self.cfg.device_name)
        mid = self.cfg.machine_id
        self.id_label.setText(f"{mid[:3]} {mid[3:6]} {mid[6:]}")
        self.pw.setText(self.cfg.password if self.cfg.permanent_enabled else "")
        self.pw.setPlaceholderText("" if self.cfg.permanent_enabled else "غیرفعال")
        self.pw.setEnabled(self.cfg.permanent_enabled)

    def apply_settings(self) -> None:
        self.clipboard.enabled = self.cfg.share_clipboard
        self.services.dest_dir = self.cfg.dest_dir()
        self.services.max_file_bytes = self.cfg.max_file_mb << 20
        self.refresh_credentials()
        if self.host:
            self.host.otp.ttl = self.cfg.otp_ttl_minutes * 60
            self.host.otp.length = self.cfg.otp_length
            self.loop.call_soon_threadsafe(self.host.start_relay)
            self._log("تنظیمات اعمال شد (تغییر پورت/کشف شبکه پس از شروع دوباره‌ی اشتراک اعمال می‌شود)")

    def _new_otp(self) -> None:
        if not self.host:
            return
        self._otp_pw = self.host.otp.create()
        self.otp.setText(self._otp_pw)
        self._log("رمز یکبارمصرف ساخته شد؛ فقط برای یک ورود معتبر است")
        self._tick_otp()

    def _tick_otp(self) -> None:
        if not (self.host and self._otp_pw):
            return
        rem = self.host.otp.remaining(self._otp_pw)
        if rem > 0:
            self.otp_btn.setText(f"رمز جدید ({int(rem // 60)}:{int(rem % 60):02d})")
        else:
            self.otp.clear()
            self.otp.setPlaceholderText("استفاده شد یا منقضی شد")
            self.otp_btn.setText("ساخت رمز یکبارمصرف")
            self._otp_pw = None

    def _set_relay(self, state: str) -> None:
        text = {
            "off": "خاموش — فقط شبکه‌ی محلی",
            "connecting": "در حال اتصال…",
            "online": "آنلاین ✔ — از اینترنت قابل دسترسی است",
            "offline": "قطع — تلاش دوباره…",
        }.get(state)
        if text is None and state.startswith("error:"):
            code = state[6:]
            text = {"bad_token": "توکن اشتباه است", "id_taken": "شناسه تکراری بود؛ شناسه‌ی جدید گرفته شد",
                    "auth": "احراز هویت ریلی ناموفق"}.get(code, f"خطا: {code}")
        self.relay_lbl.setText(text or state)
        self.refresh_credentials()

    def _kick(self) -> None:
        if self.host:
            self.loop.call_soon_threadsafe(self.host.kick_all)

    def _send_file(self) -> None:
        if not self.host:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "ارسال فایل به بیننده")
        for p in paths:
            asyncio.run_coroutine_threadsafe(self.host.send_file(p), self.loop)

    def shutdown(self) -> None:
        if self.host:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.host.stop(), self.loop).result(timeout=4)
            except Exception:
                pass
        self.cfg.save()
        self.loop.call_soon_threadsafe(self.loop.stop)


# ---------------------------------------------------------------------------
# Viewer panel
# ---------------------------------------------------------------------------

class ViewerPanel(QWidget):
    note = pyqtSignal(str)

    def __init__(self, cfg: Config, book: AddressBook, parent=None):
        super().__init__(parent)
        self.cfg, self.book = cfg, book
        self._args: Optional[tuple] = None
        self._frames = 0
        self._res = ""
        self._ping = None
        self._settings: dict = {}
        self._first_info = True
        self.prompter = Prompter(self)
        self.clipboard = ClipboardBridge()
        self.services = Services(
            clipboard=self.clipboard, ask=self.prompter.ask_async,
            notify=self.note.emit, dest_dir=default_download_dir())
        self.note.connect(self._on_note)
        self.session: Optional[RemoteSession] = None
        self.fwd = InputForwarder(None)  # type: ignore
        self._build()

    def _build(self) -> None:
        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)

        form_w = QWidget()
        form_l = QVBoxLayout(form_w)
        form_l.setSpacing(16)
        form_l.addStretch(1)

        title = QLabel("اتصال به کامپیوتر")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form_l.addWidget(title)

        card = QGroupBox()
        fl = QFormLayout(card)
        fl.setSpacing(12)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("آدرس IP یا شناسه ۹ رقمی")
        self.nearby = QComboBox()
        self.nearby.addItem("(اسکن نشده)")
        scan = QPushButton("جستجو در شبکه")
        scan.clicked.connect(self._scan)
        row = QHBoxLayout()
        row.addWidget(self.nearby, 1)
        row.addWidget(scan)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(7070)
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_edit.setPlaceholderText("رمز عبور")
        fl.addRow("میزبان / شناسه:", self.host_edit)
        fl.addRow("نزدیک:", row)
        self.book_combo = QComboBox()
        self.book_combo.currentIndexChanged.connect(self._book_picked)
        save_btn = QPushButton("ذخیره…")
        save_btn.clicked.connect(self._save_to_book)
        brow = QHBoxLayout()
        brow.addWidget(self.book_combo, 1)
        brow.addWidget(save_btn)
        fl.addRow("دفترچه آدرس:", brow)
        self._reload_book()
        fl.addRow("پورت:", self.port_spin)
        fl.addRow("رمز عبور:", self.pw_edit)
        form_l.addWidget(card)

        self.connect_btn = QPushButton("اتصال")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setMinimumHeight(44)
        self.connect_btn.clicked.connect(self._do_connect)
        form_l.addWidget(self.connect_btn)
        form_l.addStretch(2)

        self.stack.addWidget(form_w)

        view_w = QWidget()
        vl = QVBoxLayout(view_w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        self.view_status = QLabel("")
        self.view_status.setObjectName("subtitle")
        disc = QPushButton("قطع اتصال")
        disc.setObjectName("danger")
        disc.clicked.connect(self._disconnect)
        send = QPushButton("ارسال فایل…")
        send.clicked.connect(self._send_files)
        bar.addWidget(self.view_status, 1)
        bar.addWidget(send)
        bar.addWidget(disc)
        vl.addLayout(bar)

        self.view = RemoteView(self.fwd)
        vl.addWidget(self.view, 1)
        self.stack.addWidget(view_w)

        self.nearby.currentIndexChanged.connect(self._picked)

    def _reload_book(self) -> None:
        self.book_combo.blockSignals(True)
        self.book_combo.clear()
        self.book_combo.addItem("(انتخاب از دفترچه)")
        for e in self.book.sorted():
            tag = f"  [{'، '.join(e.tags)}]" if e.tags else ""
            self.book_combo.addItem(f"{e.title}  ({e.key}){tag}", e)
        self.book_combo.blockSignals(False)

    def _book_picked(self, _i: int) -> None:
        e = self.book_combo.currentData()
        if e:
            self.host_edit.setText(e.key)
            self.port_spin.setValue(e.port or 7070)

    def _save_to_book(self) -> None:
        key = re.sub(r"\s", "", self.host_edit.text().strip())
        if not key:
            return
        cur = self.book.entries.get(key)
        label, ok = QInputDialog.getText(self, "ذخیره در دفترچه", "برچسب این دستگاه:",
                                         text=cur.label if cur else "")
        if not ok:
            return
        e = cur or Entry(key)
        e.label = label.strip()
        e.port = self.port_spin.value() if self.port_spin.value() != 7070 else 0
        self.book.upsert(e)
        self._reload_book()

    def _scan(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = discover()
        finally:
            QApplication.restoreOverrideCursor()
        self.nearby.clear()
        if not found:
            self.nearby.addItem("(میزبانی یافت نشد)")
            return
        self.nearby.addItem("یک میزبان انتخاب کنید…")
        for h in found:
            self.nearby.addItem(f"{h['name']}  ({h['id']})  {h['host']}", h)

    def _picked(self, _i: int) -> None:
        h = self.nearby.currentData()
        if h:
            self.host_edit.setText(h["host"])
            self.port_spin.setValue(h["port"])

    def _do_connect(self) -> None:
        target = self.host_edit.text().strip()
        if not target:
            return
        password = self.pw_edit.text()
        if not password:
            QMessageBox.warning(self, "ViunaLink", "لطفاً رمز عبور را وارد کنید.")
            return

        relay = RelaySettings(self.cfg.relay_server, self.cfg.relay_port,
                              self.cfg.relay_token, self.cfg.relay_tls)
        self._args = (target, self.port_spin.value(), password, relay)
        self.book.touch(re.sub(r"\s", "", target))
        self._start_session()

    def _start_session(self) -> None:
        assert self._args
        self.session = RemoteSession(self.services)
        self.fwd.session = self.session
        self._first_info = True
        self._frames = 0
        s = self.session
        s.frame_ready.connect(self._on_frame)
        s.info.connect(self._on_info)
        s.status.connect(self._on_note)
        s.latency.connect(self._on_latency)
        s.closed.connect(self._on_closed)
        s.start(*self._args)
        self.stack.setCurrentIndex(1)
        self.view_status.setText(f"در حال اتصال به {self._args[0]}…")

    def _on_note(self, msg: str) -> None:
        self.view_status.setText(msg)

    def _on_latency(self, ms: float) -> None:
        self._ping = ms

    def _on_info(self, info: dict) -> None:
        self._res = f"{info['width']}×{info['height']}"
        name = info.get("name") or (self._args[0] if self._args else "")
        mode = {"relay": "ریلی", "lan": "شبکه محلی", "direct": "مستقیم"}.get(
            getattr(self.session, "mode", ""), "")
        self.view_status.setText(f"{name}  ·  {self._res}" + (f"  ·  {mode}" if mode else ""))
        if self._first_info and self._settings:
            self._first_info = False
            self.session.request_settings(**self._settings, keyframe=True)

    def _on_frame(self) -> None:
        if self.session is None:
            return
        img = self.session.take_frame()
        if img is not None:
            self._frames += 1
            self.view.set_image(img)
            if not self.view.hasFocus():
                self.view.setFocus()

    def _on_closed(self, reason: str) -> None:
        if not reason:
            return
        ans = QMessageBox.question(self, "ViunaLink", f"{reason}\n\nدوباره وصل شویم؟")
        if ans == QMessageBox.StandardButton.Yes and self._args:
            self._start_session()
        else:
            self._disconnect()

    def _disconnect(self) -> None:
        if self.session:
            self.session.close()
            self.session = None
        self.view.set_image(None)
        self.stack.setCurrentIndex(0)

    def _send_files(self) -> None:
        if not self.session:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "ارسال فایل به میزبان")
        for p in paths:
            self.session.send_file(p)

    def tick(self) -> None:
        if self.stack.currentIndex() != 1:
            return
        ping = f"  ·  {self._ping:.0f} ms" if self._ping is not None else ""
        base = self.view_status.text().split("  ·  ")[0] if self.view_status.text() else ""
        if self._res:
            self.view_status.setText(f"{base}  ·  {self._res}  ·  {self._frames} fps{ping}")
        self._frames = 0

    def shutdown(self) -> None:
        if self.session:
            self.session.close()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.cfg = cfg or Config.load()
        self.book = AddressBook()
        self.setWindowTitle("ViunaLink")
        self.resize(960, 680)
        self.setMinimumSize(720, 520)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        side = QFrame()
        side.setFixedWidth(200)
        side.setStyleSheet("QFrame { background: #181825; border-left: 1px solid #313244; }")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 16, 12, 16)
        sl.setSpacing(8)

        logo = QLabel("ViunaLink")
        logo.setObjectName("title")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(logo)
        ver = QLabel("نسخه ۱.۲")
        ver.setObjectName("subtitle")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(ver)
        author = QLabel("نویسنده: Banshee")
        author.setObjectName("subtitle")
        author.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(author)
        sl.addSpacing(20)

        self.btn_host = QPushButton("🖥  میزبان\nاشتراک این کامپیوتر")
        self.btn_host.setObjectName("modeBtn")
        self.btn_host.setProperty("active", True)
        self.btn_host.clicked.connect(lambda: self._switch(0))
        self.btn_view = QPushButton("👁  بیننده\nاتصال به کامپیوتر")
        self.btn_view.setObjectName("modeBtn")
        self.btn_view.setProperty("active", False)
        self.btn_view.clicked.connect(lambda: self._switch(1))
        sl.addWidget(self.btn_host)
        sl.addWidget(self.btn_view)
        self.btn_settings = QPushButton("⚙  تنظیمات\nقفل‌دار با رمز")
        self.btn_settings.setObjectName("modeBtn")
        self.btn_settings.clicked.connect(self._open_settings)
        sl.addWidget(self.btn_settings)
        sl.addStretch(1)

        tip = QLabel("نکته: میزبان و بیننده\nدر یک پنجره هستند.\nهر وقت خواستید عوض کنید.")
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(tip)

        outer.addWidget(side)

        self.content = QStackedWidget()
        self.host_panel = HostPanel(self.cfg)
        self.viewer_panel = ViewerPanel(self.cfg, self.book)
        self.content.addWidget(self.host_panel)
        self.content.addWidget(self.viewer_panel)
        outer.addWidget(self.content, 1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("آماده")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.viewer_panel.tick)
        self._timer.start(1000)

        self._switch(0)

    def _switch(self, index: int) -> None:
        self.content.setCurrentIndex(index)
        self.btn_host.setProperty("active", index == 0)
        self.btn_view.setProperty("active", index == 1)
        for b in (self.btn_host, self.btn_view):
            b.style().unpolish(b)
            b.style().polish(b)
        self.status.showMessage("حالت میزبان" if index == 0 else "حالت بیننده")

    def _open_settings(self) -> None:
        if not ask_settings_access(self, self.cfg):
            return
        dlg = SettingsDialog(self.cfg, self.book, self)
        if dlg.exec():
            self.host_panel.apply_settings()
            self.viewer_panel._reload_book()
            self.status.showMessage("تنظیمات ذخیره شد", 4000)

    def closeEvent(self, e):
        self.host_panel.shutdown()
        self.viewer_panel.shutdown()
        e.accept()


def run_app(cfg: Config | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_dark_theme(app)
    win = MainWindow(cfg)
    win.show()
    return app.exec()

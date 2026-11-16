"""PyQt6 window for the host: shows ID/password, controls sharing."""
from __future__ import annotations

import asyncio
import sys
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                             QPushButton, QSpinBox, QVBoxLayout, QWidget)

from core import security
from core.services import Services
from server.host import Host
from utils.config import Config
from utils.helpers import local_ips, session_warning
from utils.qt_bridge import ClipboardBridge, Prompter


def _list_monitors() -> list[tuple[int, int, int]]:
    try:
        import mss
        with mss.mss() as s:
            return [(i, m["width"], m["height"]) for i, m in enumerate(s.monitors)]
    except Exception:
        return [(1, 0, 0)]


class HostWindow(QMainWindow):
    note = pyqtSignal(str)
    peers = pyqtSignal(list)
    started = pyqtSignal(object, object)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.host: Host | None = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True,
                         name="remotelink-host").start()
        self.prompter = Prompter(self)
        self.clipboard = ClipboardBridge()
        self.clipboard.enabled = cfg.share_clipboard
        self.services = Services(
            clipboard=self.clipboard, ask=self.prompter.ask_async,
            notify=self.note.emit, sessions_changed=self.peers.emit,
            dest_dir=cfg.dest_dir(), max_file_bytes=cfg.max_file_mb << 20)
        self.note.connect(self._log)
        self.peers.connect(self._set_peers)
        self.started.connect(self._on_started)
        self._build()
        self.addr.setText(", ".join(f"{ip}:{cfg.port}" for ip in local_ips()) or "n/a")
        warn = session_warning()
        if warn:
            self._log(warn)

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        self.setWindowTitle("RemoteLink - Host")
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)

        box = QGroupBox("Your computer")
        f = QFormLayout(box)
        mid = self.cfg.machine_id
        self.id_label = QLabel(f"<b style='font-size:20px'>{mid[:3]} {mid[3:6]} {mid[6:]}</b>")
        self.id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pw = QLineEdit(self.cfg.password)
        self.pw.setReadOnly(True)
        new_pw = QPushButton("New password")
        new_pw.clicked.connect(self._new_password)
        row = QHBoxLayout()
        row.addWidget(self.pw, 1)
        row.addWidget(new_pw)
        self.addr = QLabel("")
        self.addr.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f.addRow("ID:", self.id_label)
        f.addRow("Password:", row)
        f.addRow("Address:", self.addr)
        lay.addWidget(box)

        opt = QGroupBox("Options (apply to new connections)")
        g = QFormLayout(opt)
        self.monitor = QComboBox()
        for i, w, h in _list_monitors():
            self.monitor.addItem("All monitors" if i == 0 else f"Monitor {i} ({w}x{h})", i)
        k = self.monitor.findData(self.cfg.monitor)
        self.monitor.setCurrentIndex(max(0, k))
        self.monitor.currentIndexChanged.connect(
            lambda _i: setattr(self.cfg, "monitor", self.monitor.currentData()))
        self.quality = self._spin(10, 95, self.cfg.jpeg_quality, "jpeg_quality")
        self.fps = self._spin(1, 60, self.cfg.fps, "fps")
        self.port = self._spin(1, 65535, self.cfg.port, "port")
        g.addRow("Monitor:", self.monitor)
        g.addRow("Quality:", self.quality)
        g.addRow("Frames/s:", self.fps)
        g.addRow("TCP port:", self.port)
        for text, attr in (("Ask before allowing a connection", "confirm_connections"),
                           ("Share clipboard", "share_clipboard"),
                           ("Discoverable on this network (lets viewers find me by ID)",
                            "discoverable")):
            c = QCheckBox(text)
            c.setChecked(getattr(self.cfg, attr))
            c.toggled.connect(lambda on, a=attr: self._flag(a, on))
            g.addRow(c)
        lay.addWidget(opt)

        self.state = QLabel("Stopped")
        self.btn = QPushButton("Start sharing")
        self.btn.setMinimumHeight(40)
        self.btn.clicked.connect(self._toggle)
        self.kick = QPushButton("Disconnect viewer")
        self.kick.clicked.connect(self._kick)
        self.sendf = QPushButton("Send file to viewer...")
        self.sendf.clicked.connect(self._send_file)
        self.kick.setEnabled(False)
        self.sendf.setEnabled(False)
        btns = QHBoxLayout()
        btns.addWidget(self.btn, 2)
        btns.addWidget(self.kick, 1)
        btns.addWidget(self.sendf, 1)
        lay.addWidget(self.state)
        lay.addLayout(btns)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(500)
        lay.addWidget(self.logbox, 1)
        self.resize(520, 640)

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

    # ------------------------------------------------------------- actions
    def _log(self, msg: str) -> None:
        self.logbox.appendPlainText(msg)

    def _new_password(self) -> None:
        self.cfg.password = security.generate_password()
        self.pw.setText(self.cfg.password)
        self.cfg.save()
        self._log("New password generated")

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
            self._log("Sharing stopped")

    def _on_started(self, host, exc) -> None:
        self.btn.setEnabled(True)
        if exc is not None:
            QMessageBox.critical(self, "RemoteLink", f"Cannot start sharing:\n{exc}")
            return
        self.host = host
        self.cfg.save()
        self._running(True)
        self._log(f"Sharing started on port {self.cfg.port}")

    def _running(self, on: bool) -> None:
        self.btn.setText("Stop sharing" if on else "Start sharing")
        self.port.setEnabled(not on)
        self.state.setText("Sharing - waiting for a viewer" if on else "Stopped")
        ips = ", ".join(f"{ip}:{self.cfg.port}" for ip in local_ips())
        self.addr.setText(ips or "n/a")
        if not on:
            self.kick.setEnabled(False)
            self.sendf.setEnabled(False)

    def _set_peers(self, peers: list) -> None:
        if self.host is None:
            return
        if peers:
            self.state.setText(f"Viewer connected: {', '.join(peers)}")
        else:
            self.state.setText("Sharing - waiting for a viewer")
        self.kick.setEnabled(bool(peers))
        self.sendf.setEnabled(bool(peers))

    def _kick(self) -> None:
        if self.host:
            self.loop.call_soon_threadsafe(self.host.kick_all)

    def _send_file(self) -> None:
        if not self.host:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Send files to viewer")
        for p in paths:
            asyncio.run_coroutine_threadsafe(self.host.send_file(p), self.loop)

    def closeEvent(self, e):
        if self.host:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.host.stop(), self.loop).result(timeout=4)
            except Exception:
                pass
        self.cfg.save()
        self.loop.call_soon_threadsafe(self.loop.stop)
        e.accept()


def run_host_gui(cfg: Config | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = HostWindow(cfg or Config.load())
    win.show()
    return app.exec()

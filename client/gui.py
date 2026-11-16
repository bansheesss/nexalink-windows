"""PyQt6 viewer window, connect dialog and the start-up launcher."""
from __future__ import annotations

import re
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QPainter
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox, QPushButton,
                             QSpinBox, QVBoxLayout, QWidget)

from client.input_handler import InputForwarder
from client.viewer import RemoteSession
from core.discovery import discover
from core.services import Services, default_download_dir
from utils.qt_bridge import ClipboardBridge, Prompter

_MOUSE = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.RightButton: 2,
          Qt.MouseButton.MiddleButton: 3}
_ID_RE = re.compile(r"\d{3}\s?\d{3}\s?\d{3}")


def _app() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv[:1])


class ConnectDialog(QDialog):
    def __init__(self, host="", port=7070, password=""):
        super().__init__()
        self.setWindowTitle("RemoteLink - Connect")
        self.host = QLineEdit(host)
        self.host.setPlaceholderText("IP address or 9-digit ID")
        self.nearby = QComboBox()
        self.nearby.addItem("(none scanned)")
        scan = QPushButton("Find on LAN")
        scan.setAutoDefault(False)
        scan.clicked.connect(self._scan)
        self.nearby.currentIndexChanged.connect(self._picked)
        row = QHBoxLayout()
        row.addWidget(self.nearby, 1)
        row.addWidget(scan)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(port)
        self.password = QLineEdit(password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form = QFormLayout(self)
        form.addRow("Host:", self.host)
        form.addRow("Nearby:", row)
        form.addRow("Port:", self.port)
        form.addRow("Password:", self.password)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                               QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def _scan(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = discover()
        finally:
            QApplication.restoreOverrideCursor()
        self.nearby.clear()
        if not found:
            self.nearby.addItem("(no hosts found)")
            return
        self.nearby.addItem("Select a host...")
        for h in found:
            self.nearby.addItem(f"{h['name']}  ({h['id']})  {h['host']}", h)

    def _picked(self, _i: int) -> None:
        h = self.nearby.currentData()
        if h:
            self.host.setText(h["host"])
            self.port.setValue(h["port"])

    def accept(self) -> None:
        target = self.host.text().strip()
        if _ID_RE.fullmatch(target):             # a 9-digit ID, not an IP
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                found = discover(re.sub(r"\s", "", target))
            finally:
                QApplication.restoreOverrideCursor()
            if not found:
                QMessageBox.warning(
                    self, "RemoteLink",
                    "No host with that ID answered on this network.\n"
                    "Use its IP address instead (IDs work on the local network only).")
                return
            self.host.setText(found[0]["host"])
            self.port.setValue(found[0]["port"])
        if not self.host.text().strip():
            return
        super().accept()

    def values(self):
        return self.host.text().strip(), self.port.value(), self.password.text()


class RemoteView(QWidget):
    def __init__(self, forwarder: InputForwarder):
        super().__init__()
        self.fwd = forwarder
        self.image = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(320, 200)

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
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.image is not None:
            p.drawImage(self._target(), self.image)

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
            self.fwd.move(*n, force=True)       # land exactly where we click
            self.fwd.button(b, pressed)

    def wheelEvent(self, e):
        d = e.angleDelta()
        step = lambda v: 0 if v == 0 else (v // 120 or (1 if v > 0 else -1))
        self.fwd.scroll(step(d.x()), step(d.y()))

    def keyPressEvent(self, e):
        self.fwd.key(e, True)

    def keyReleaseEvent(self, e):
        self.fwd.key(e, False)

    def focusNextPrevChild(self, _next):        # deliver Tab to the remote side
        return False

    def focusOutEvent(self, e):
        self.fwd.release_all()
        super().focusOutEvent(e)


class ViewerWindow(QMainWindow):
    note = pyqtSignal(str)

    def __init__(self, host: str, port: int, password: str):
        super().__init__()
        self._args = (host, port, password)
        self._frames, self._res, self._ping = 0, "", None
        self._settings: dict = {}
        self._first_info = True
        self._mon_group = None
        self.prompter = Prompter(self)
        self.clipboard = ClipboardBridge()
        self.services = Services(
            clipboard=self.clipboard, ask=self.prompter.ask_async,
            notify=self.note.emit, dest_dir=default_download_dir())
        self.note.connect(self._on_note)
        self.session = RemoteSession(self.services)
        self.fwd = InputForwarder(self.session)
        self.view = RemoteView(self.fwd)
        self.setCentralWidget(self.view)
        self.resize(1024, 640)
        self.setWindowTitle(f"RemoteLink - {host}")
        self.status_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_label)
        self._build_menus()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._start()

    # ---- menus ----
    def _build_menus(self) -> None:
        bar = self.menuBar()
        fm = bar.addMenu("&File")
        send = QAction("Send file(s) to host...", self)
        send.triggered.connect(self._send_files)
        fm.addAction(send)
        fm.addSeparator()
        disc = QAction("Disconnect", self)
        disc.triggered.connect(self.close)
        fm.addAction(disc)

        om = bar.addMenu("&Options")
        clip = QAction("Sync clipboard", self)
        clip.setCheckable(True)
        clip.setChecked(True)
        clip.toggled.connect(self._set_clipboard)
        om.addAction(clip)
        self._radio(om, "Quality", "quality",
                    [("Low (40)", 40), ("Medium (60)", 60), ("High (80)", 80)], 60)
        self._radio(om, "Frame rate", "fps",
                    [("10 fps", 10), ("20 fps", 20), ("30 fps", 30)], 20)
        self._radio(om, "Resolution", "scale",
                    [("100%", 1.0), ("75%", 0.75), ("50%", 0.5)], 1.0)
        self.mon_menu = om.addMenu("Monitor")

    def _radio(self, menu, title, key, items, default) -> None:
        sub = menu.addMenu(title)
        grp = QActionGroup(self)
        for label, val in items:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setChecked(val == default)
            grp.addAction(a)
            sub.addAction(a)
            a.triggered.connect(lambda _=False, k=key, v=val: self._setting(k, v))

    def _setting(self, key, val) -> None:
        self._settings[key] = val
        self.session.request_settings(**{key: val, "keyframe": True})

    def _set_clipboard(self, on: bool) -> None:
        self.clipboard.enabled = on

    def _send_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Send files to host")
        for p in paths:
            self.session.send_file(p)

    # ---- session ----
    def _start(self) -> None:
        self.session = RemoteSession(self.services)
        self.fwd.session = self.session
        self._first_info = True
        s = self.session
        s.frame_ready.connect(self._on_frame)
        s.info.connect(self._on_info)
        s.status.connect(self._on_note)
        s.latency.connect(self._on_latency)
        s.closed.connect(self._on_closed)
        s.start(*self._args)

    def _on_note(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 8000)

    def _on_latency(self, ms: float) -> None:
        self._ping = ms

    def _on_info(self, info: dict) -> None:
        self._res = f"{info['width']}x{info['height']}"
        self.setWindowTitle(f"RemoteLink - {info.get('name') or self._args[0]}")
        if self._first_info and self._settings:      # keep choices across reconnects
            self._first_info = False
            self.session.request_settings(**self._settings, keyframe=True)
        if self._mon_group:
            self._mon_group.deleteLater()
        self.mon_menu.clear()
        self._mon_group = QActionGroup(self)
        for m in info.get("monitors", []):
            label = "All monitors" if m["index"] == 0 else f"Monitor {m['index']}"
            a = QAction(f"{label} ({m['width']}x{m['height']})", self)
            a.setCheckable(True)
            a.setChecked(m["index"] == info.get("monitor"))
            self._mon_group.addAction(a)
            self.mon_menu.addAction(a)
            a.triggered.connect(
                lambda _=False, i=m["index"]: self.session.request_settings(monitor=i))

    def _on_frame(self) -> None:
        img = self.session.take_frame()
        if img is not None:
            self._frames += 1
            self.view.set_image(img)
            if not self.view.hasFocus():
                self.view.setFocus()

    def _tick(self) -> None:
        ping = f"  {self._ping:.0f} ms" if self._ping is not None else ""
        self.status_label.setText(f"{self._res}  {self._frames} fps{ping}")
        self._frames = 0

    def _on_closed(self, reason: str) -> None:
        if not reason:                          # we closed it ourselves
            return
        ans = QMessageBox.question(self, "RemoteLink", f"{reason}\n\nReconnect?")
        if ans == QMessageBox.StandardButton.Yes:
            self._start()
        else:
            self.close()

    def closeEvent(self, e):
        self.session.close()
        e.accept()


def run_client_gui(host=None, port=7070, password=None) -> int:
    app = _app()
    if not host or not password:
        dlg = ConnectDialog(host or "", port, password or "")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
        host, port, password = dlg.values()
    elif _ID_RE.fullmatch(host):
        found = discover(re.sub(r"\s", "", host))
        if not found:
            print("No host with that ID answered on this network.")
            return 1
        host, port = found[0]["host"], found[0]["port"]
    win = ViewerWindow(host, port, password)
    win.show()
    return app.exec()


def run_launcher() -> int:
    app = _app()
    dlg = QDialog()
    dlg.setWindowTitle("RemoteLink")
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel("What do you want to do?"))
    choice: dict = {}
    for text, name in (("Share this computer (Host)", "host"),
                       ("Connect to a computer (Viewer)", "client")):
        b = QPushButton(text)
        b.setMinimumHeight(48)
        b.clicked.connect(lambda _=False, n=name: (choice.__setitem__("v", n), dlg.accept()))
        lay.addWidget(b)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return 0
    if choice["v"] == "host":
        from server.host_gui import run_host_gui
        return run_host_gui()
    return run_client_gui()

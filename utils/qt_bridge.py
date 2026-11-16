"""Thread bridges between the asyncio network thread and the Qt GUI thread."""
from __future__ import annotations

import asyncio
import concurrent.futures

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox


class ClipboardBridge(QObject):
    """Text clipboard sync. Must be created on the GUI thread."""
    local_changed = pyqtSignal(str)
    _apply_sig = pyqtSignal(str)
    MAX_BYTES = 1 << 20

    def __init__(self):
        super().__init__()
        self.enabled = True
        self._last = QApplication.clipboard().text()
        QApplication.clipboard().dataChanged.connect(self._on_changed)
        self._apply_sig.connect(self._apply)

    def _on_changed(self) -> None:
        if not self.enabled:
            return
        text = QApplication.clipboard().text()
        if text and text != self._last and len(text.encode("utf-8")) <= self.MAX_BYTES:
            self._last = text                 # also stops echo of remote text
            self.local_changed.emit(text)

    def subscribe(self, cb) -> None:
        self.local_changed.connect(cb)

    def unsubscribe(self, cb) -> None:
        try:
            self.local_changed.disconnect(cb)
        except (TypeError, RuntimeError):
            pass

    def set_text(self, text: str) -> None:    # any thread
        if self.enabled:
            self._apply_sig.emit(text)

    def _apply(self, text: str) -> None:
        self._last = text
        QApplication.clipboard().setText(text)


class Prompter(QObject):
    """Lets the asyncio thread ask the user a yes/no question."""
    _ask = pyqtSignal(str, str, object)

    def __init__(self, parent_widget=None):
        super().__init__()
        self._parent = parent_widget
        self._ask.connect(self._show)

    def _show(self, title: str, text: str, fut) -> None:
        ans = QMessageBox.question(self._parent, title, text)
        fut.set_result(ans == QMessageBox.StandardButton.Yes)

    async def ask_async(self, title: str, text: str) -> bool:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._ask.emit(title, text, fut)
        return await asyncio.wrap_future(fut)

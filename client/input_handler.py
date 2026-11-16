"""Translates Qt input events into RemoteLink input messages."""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt

from core.protocol import (MsgType, pack_key, pack_mouse_button,
                           pack_mouse_move, pack_scroll)

_K = Qt.Key
# Names match pynput.keyboard.Key members, so Windows <-> Linux just works.
_SPECIAL = {k.value: n for k, n in {
    _K.Key_Return: "enter", _K.Key_Enter: "enter", _K.Key_Escape: "esc",
    _K.Key_Tab: "tab", _K.Key_Backtab: "tab", _K.Key_Backspace: "backspace",
    _K.Key_Delete: "delete", _K.Key_Insert: "insert", _K.Key_Home: "home",
    _K.Key_End: "end", _K.Key_PageUp: "page_up", _K.Key_PageDown: "page_down",
    _K.Key_Up: "up", _K.Key_Down: "down", _K.Key_Left: "left",
    _K.Key_Right: "right", _K.Key_Space: "space", _K.Key_Shift: "shift",
    _K.Key_Control: "ctrl", _K.Key_Alt: "alt", _K.Key_AltGr: "alt_gr",
    _K.Key_Meta: "cmd", _K.Key_CapsLock: "caps_lock",
}.items()}
_F1 = _K.Key_F1.value


def qt_key_to_name(event) -> str | None:
    k = event.key()
    if k in _SPECIAL:
        return _SPECIAL[k]
    if _F1 <= k <= _F1 + 11:
        return f"f{k - _F1 + 1}"
    if 0x20 <= k <= 0x7E:                       # printable ASCII key codes
        ch = chr(k)
        return ch.lower() if ch.isalpha() else ch
    text = event.text()
    if len(text) == 1 and text.isprintable():
        return text
    return None


class InputForwarder:
    MOVE_INTERVAL = 1 / 90                      # cap mouse-move rate

    def __init__(self, session):
        self.session = session
        self._last_move = 0.0
        self._down: set[str] = set()

    def move(self, nx: float, ny: float, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_move < self.MOVE_INTERVAL:
            return
        self._last_move = now
        self.session.send(MsgType.MOUSE_MOVE, pack_mouse_move(nx, ny))

    def button(self, button: int, pressed: bool) -> None:
        self.session.send(MsgType.MOUSE_BUTTON, pack_mouse_button(button, pressed))

    def scroll(self, dx: int, dy: int) -> None:
        self.session.send(MsgType.MOUSE_SCROLL, pack_scroll(dx, dy))

    def key(self, event, pressed: bool) -> None:
        if event.isAutoRepeat():                # the host OS does its own repeat
            return
        name = qt_key_to_name(event)
        if name is None:
            return
        (self._down.add if pressed else self._down.discard)(name)
        self.session.send(MsgType.KEY, pack_key(name, pressed))

    def release_all(self) -> None:
        """Avoid stuck keys on the host when the view loses focus."""
        for name in list(self._down):
            self.session.send(MsgType.KEY, pack_key(name, False))
        self._down.clear()

"""Replays viewer input on the host with pynput."""
from __future__ import annotations

import logging

from pynput.keyboard import Controller as Keyboard
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as Mouse

log = logging.getLogger("remotelink.input")
_BUTTONS = {1: Button.left, 2: Button.right, 3: Button.middle}


class InputReceiver:
    def __init__(self):
        self._mouse = Mouse()
        self._kb = Keyboard()
        self._mon: dict | None = None
        self._keys_down: set = set()
        self._buttons_down: set = set()

    def set_monitor(self, monitor: dict) -> None:
        self._mon = monitor

    def move(self, nx: float, ny: float) -> None:
        if not self._mon:
            return
        m = self._mon
        self._mouse.position = (m["left"] + round(nx * (m["width"] - 1)),
                                m["top"] + round(ny * (m["height"] - 1)))

    def button(self, button: int, pressed: bool) -> None:
        b = _BUTTONS.get(button)
        if b is None:
            return
        if pressed:
            self._mouse.press(b)
            self._buttons_down.add(b)
        else:
            self._mouse.release(b)
            self._buttons_down.discard(b)

    def scroll(self, dx: int, dy: int) -> None:
        self._mouse.scroll(dx, dy)

    @staticmethod
    def _resolve(name: str):
        if len(name) == 1:
            return name
        try:
            return Key[name]          # only real pynput Key names are accepted
        except KeyError:
            return None

    def key(self, name: str, pressed: bool) -> None:
        k = self._resolve(name)
        if k is None:
            log.debug("unknown key %r", name)
            return
        try:
            if pressed:
                self._kb.press(k)
                self._keys_down.add(k)
            else:
                self._kb.release(k)
                self._keys_down.discard(k)
        except Exception as e:  # layout/permission problems must not kill the session
            log.debug("key %r failed: %s", name, e)

    def release_all(self) -> None:
        """Called on disconnect so no key or button stays stuck down."""
        for k in list(self._keys_down):
            try:
                self._kb.release(k)
            except Exception:
                pass
        for b in list(self._buttons_down):
            try:
                self._mouse.release(b)
            except Exception:
                pass
        self._keys_down.clear()
        self._buttons_down.clear()

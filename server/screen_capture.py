"""Screen grabbing (mss) + compression (Pillow) on a dedicated thread.

mss handles are thread-bound, so the instance is created lazily *inside*
the capture thread and every call goes through `self.executor`.
"""
from __future__ import annotations

import io
import zlib
from concurrent.futures import ThreadPoolExecutor

import mss
from PIL import Image

from core.protocol import Codec


class ScreenCapture:
    def __init__(self, monitor_index: int = 1):
        self.index = monitor_index
        self._sct = None
        self._last_crc: int | None = None
        self.monitor: dict | None = None   # {"left","top","width","height"}
        self.executor = ThreadPoolExecutor(1, thread_name_prefix="capture")

    # --- everything below must run on self.executor ---
    def _ensure(self) -> None:
        if self._sct is None:
            self._sct = mss.mss()
            self._pick(self.index)

    def _pick(self, idx: int) -> None:
        mons = self._sct.monitors
        if not 0 <= idx < len(mons):
            idx = 1 if len(mons) > 1 else 0
        self.index, self.monitor, self._last_crc = idx, dict(mons[idx]), None

    def select(self, idx: int) -> None:
        self._ensure()
        self._pick(idx)

    def info(self) -> list[dict]:
        self._ensure()
        return [{"index": i, "width": m["width"], "height": m["height"],
                 "left": m["left"], "top": m["top"]}
                for i, m in enumerate(self._sct.monitors)]

    def grab(self, codec: Codec, quality: int, scale: float, force: bool = False):
        """Returns (w, h, bytes), or None if the screen did not change."""
        self._ensure()
        shot = self._sct.grab(self.monitor)
        crc = zlib.crc32(shot.bgra)
        if not force and crc == self._last_crc:
            return None
        self._last_crc = crc
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if scale < 1.0:
            img = img.resize((max(1, int(img.width * scale)),
                              max(1, int(img.height * scale))), Image.BILINEAR)
        buf = io.BytesIO()
        if codec == Codec.WEBP:
            img.save(buf, "WEBP", quality=quality, method=0)
        else:
            img.save(buf, "JPEG", quality=quality)
        return img.width, img.height, buf.getvalue()

    def _close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    def close(self) -> None:
        try:
            self.executor.submit(self._close).result(timeout=2)
        except Exception:
            pass
        self.executor.shutdown(wait=False)

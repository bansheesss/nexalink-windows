"""Glue shared by host and viewer: callbacks into the GUI + clipboard pump."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from core.protocol import MsgType

log = logging.getLogger("remotelink.services")


from utils.paths import default_download_dir  # noqa: E402  (portable-aware)


@dataclass
class Services:
    # Clipboard bridge with subscribe(cb) / unsubscribe(cb) / set_text(text).
    clipboard: object | None = None
    # async (title, text) -> bool. None means "accept everything" (headless host).
    ask: Optional[Callable[[str, str], Awaitable[bool]]] = None
    notify: Callable[[str], None] = lambda _m: None
    sessions_changed: Callable[[list], None] = lambda _l: None
    relay_status: Callable[[str], None] = lambda _s: None
    dest_dir: Path = field(default_factory=default_download_dir)
    max_file_bytes: int = 4 << 30


class ClipboardPump:
    """Sends local clipboard changes to the peer and applies remote ones."""
    MAX_BYTES = 1 << 20

    def __init__(self, conn, clipboard):
        self.conn, self.cb = conn, clipboard
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.cb.subscribe(self._on_local)
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is None:
            return
        self.cb.unsubscribe(self._on_local)
        self._task.cancel()
        self._task = None

    def _on_local(self, text: str) -> None:          # may run on any thread
        if self._loop:
            self._loop.call_soon_threadsafe(self._q.put_nowait, text)

    async def _run(self) -> None:
        try:
            while True:
                data = (await self._q.get()).encode("utf-8")
                if len(data) <= self.MAX_BYTES:
                    await self.conn.send(MsgType.CLIPBOARD, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("clipboard pump stopped: %r", e)

    def on_remote(self, body: bytes) -> None:
        if len(body) > self.MAX_BYTES:
            return
        try:
            self.cb.set_text(body.decode("utf-8"))
        except UnicodeDecodeError:
            pass

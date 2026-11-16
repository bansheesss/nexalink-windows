"""Network side of the viewer: asyncio runs in a background thread and
talks to the Qt GUI thread through signals."""
from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from core.filetransfer import FileTransfer
from core.connect import ConnectError, open_target
from core.network import Connection, client_handshake
from core.protocol import Frame, MsgType, ProtocolError, pack_json, unpack_json
from core.security import AuthError
from core.services import ClipboardPump, Services

log = logging.getLogger("remotelink.viewer")


class RemoteSession(QObject):
    frame_ready = pyqtSignal()      # a new image is waiting in take_frame()
    info = pyqtSignal(dict)
    status = pyqtSignal(str)
    latency = pyqtSignal(float)     # round-trip ms
    closed = pyqtSignal(str)        # human-readable reason ("" = we closed it)

    def __init__(self, services: Services | None = None):
        super().__init__()
        self.svc = services or Services()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conn: Connection | None = None
        self._files: FileTransfer | None = None
        self._latest: QImage | None = None
        self._pending = False
        self._user_closed = False
        self.mode = ""

    # ---- GUI-thread API ----
    def start(self, host: str, port: int, password: str, relay=None) -> None:
        threading.Thread(target=self._thread_main, args=(host, port, password, relay),
                         daemon=True, name="remotelink-net").start()

    def take_frame(self) -> QImage | None:
        self._pending = False           # must precede the read (see _run)
        return self._latest

    def send(self, msg_type: MsgType, body: bytes = b"") -> None:
        loop, conn = self._loop, self._conn
        if loop is None or conn is None or conn.cipher is None:
            return
        asyncio.run_coroutine_threadsafe(self._safe_send(conn, msg_type, body), loop)

    def request_settings(self, **kw) -> None:
        self.send(MsgType.FRAME_REQUEST, pack_json(kw))

    def send_file(self, path) -> None:
        loop, files = self._loop, self._files
        if loop and files:
            asyncio.run_coroutine_threadsafe(files.send_file(path), loop)

    def close(self) -> None:
        self._user_closed = True
        loop, conn = self._loop, self._conn
        if loop and conn and conn.cipher:
            async def bye():
                await self._safe_send(conn, MsgType.BYE, b"")
                await conn.close()
            asyncio.run_coroutine_threadsafe(bye(), loop)

    # ---- network thread ----
    @staticmethod
    async def _safe_send(conn, t, body) -> None:
        try:
            await conn.send(t, body)
        except Exception:
            pass

    def _thread_main(self, host, port, password, relay=None) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._run(host, port, password, relay))
        finally:
            loop.close()

    async def _ping_loop(self, conn) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                await conn.send(MsgType.PING, struct.pack("!d", time.monotonic()))
        except Exception:
            pass

    async def _run(self, host, port, password, relay=None) -> None:
        reason = "Disconnected"
        conn = None
        pump = ping = None
        try:
            self.status.emit(f"Connecting to {host}:{port}...")
            conn, mode = await open_target(host, port, relay)
            self._conn = conn
            self.mode = mode
            self.status.emit({"relay": "از طریق ریلی (اینترنت) وصل شد؛ احراز هویت…",
                              "lan": "در شبکه‌ی محلی پیدا شد؛ احراز هویت…"}.get(
                                  mode, "احراز هویت…"))
            await asyncio.wait_for(client_handshake(conn, password), 20)
            self.status.emit("Authenticated - waiting for the host...")
            self._files = FileTransfer(conn, self.svc, 1)
            if self.svc.clipboard:
                pump = ClipboardPump(conn, self.svc.clipboard)
                pump.start()
            ping = asyncio.create_task(self._ping_loop(conn))
            got_info = False
            while True:
                # the host may wait up to 30 s for its user to approve us
                t, body = await asyncio.wait_for(conn.recv(), 15 if got_info else 45)
                if t is MsgType.FRAME:
                    f = Frame.unpack(body)
                    img = QImage.fromData(f.data)
                    if not img.isNull():
                        self._latest = img.convertToFormat(QImage.Format.Format_RGB32)
                        if not self._pending:
                            self._pending = True
                            self.frame_ready.emit()
                elif t is MsgType.SESSION_INFO:
                    got_info = True
                    self.status.emit("Connected")
                    self.info.emit(unpack_json(body))
                elif t is MsgType.PING:
                    await conn.send(MsgType.PONG, body)
                elif t is MsgType.PONG:
                    if len(body) >= 8:
                        (t0,) = struct.unpack("!d", body[:8])
                        self.latency.emit((time.monotonic() - t0) * 1000)
                elif t is MsgType.CLIPBOARD:
                    if pump:
                        pump.on_remote(body)
                elif t.name.startswith("FILE_"):
                    await self._files.handle(t, body)
                elif t is MsgType.ERROR:
                    reason = unpack_json(body).get("reason", "Host error")
                    break
                elif t is MsgType.BYE:
                    break
        except ConnectError as e:
            reason = str(e)
        except AuthError:
            reason = "Authentication failed (wrong password?)"
        except asyncio.TimeoutError:
            reason = "Connection timed out"
        except OSError as e:
            reason = f"Cannot connect: {e}"
        except (asyncio.IncompleteReadError, ProtocolError):
            reason = "Connection lost"
        finally:
            if ping:
                ping.cancel()
            if pump:
                pump.stop()
            if self._files:
                self._files.close()
            if conn:
                await conn.close()
            self._conn = None
            self.closed.emit("" if self._user_closed else reason)

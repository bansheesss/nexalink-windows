"""RemoteLink host: accepts viewers, authenticates them, streams the screen."""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
import time

from core import discovery
from core.filetransfer import FileTransfer
from core.network import Connection, host_handshake
from core.otp import OtpStore
from core.relay_client import RelayHostLink, RelaySettings, identity_from_seed
from core.protocol import (Codec, Frame, MsgType, ProtocolError, pack_json,
                           unpack_json, unpack_key, unpack_mouse_button,
                           unpack_mouse_move, unpack_scroll)
from core.security import AuthError
from core.services import ClipboardPump, Services
from utils.config import Config

log = logging.getLogger("remotelink.host")

MAX_FAILS, FAIL_WINDOW = 5, 60.0          # per-IP auth rate limit
BACKPRESSURE_BYTES = 512 * 1024           # skip frames if the socket is backed up
IDLE_TIMEOUT = 30.0                       # viewers ping every 2 s
CODECS = {"jpeg": Codec.JPEG, "webp": Codec.WEBP}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Session:
    def __init__(self, conn: Connection, cfg: Config, peer: str,
                 services: Services, capture_factory, input_factory,
                 kind: str = "permanent", via: str = "lan"):
        self.conn, self.cfg, self.peer = conn, cfg, peer
        self.kind, self.via = kind, via
        # permissions are fixed for the lifetime of the session
        self.allow_control = cfg.allow_control
        self.allow_files = cfg.allow_files
        self.quality, self.fps, self.scale = cfg.jpeg_quality, cfg.fps, cfg.scale
        self.codec = CODECS.get(cfg.codec, Codec.JPEG)
        self.cap = capture_factory()
        self.inp = input_factory()
        self.files = FileTransfer(conn, services, 0)
        self._clip = (ClipboardPump(conn, services.clipboard)
                      if services.clipboard and cfg.share_clipboard else None)
        self._force = False
        self._frame_id = 0

    def kick(self) -> None:
        self.conn.writer.close()

    async def _grab(self, force: bool):
        return await asyncio.get_running_loop().run_in_executor(
            self.cap.executor, self.cap.grab, self.codec, self.quality,
            self.scale, force)

    async def _send_frame(self, res) -> None:
        w, h, data = res
        self._frame_id += 1
        frame = Frame(self._frame_id, int(time.monotonic() * 1000),
                      self.codec, w, h, data)
        await self.conn.send(MsgType.FRAME, frame.pack())

    async def _send_info(self) -> None:
        mons = await asyncio.get_running_loop().run_in_executor(
            self.cap.executor, self.cap.info)
        mon = self.cap.monitor
        await self.conn.send(MsgType.SESSION_INFO, pack_json({
            "width": mon["width"], "height": mon["height"],
            "os": sys.platform, "codec": self.codec.name.lower(),
            "name": self.cfg.device_name,
            "monitor": self.cap.index, "monitors": mons,
            "perms": {"control": self.allow_control, "files": self.allow_files,
                      "clipboard": self._clip is not None}}))

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        clip_started = False
        try:
            first = await self._grab(True)
            self.inp.set_monitor(self.cap.monitor)
            await self._send_info()
            await self._send_frame(first)
            if self._clip:
                self._clip.start()
                clip_started = True
            tasks = [asyncio.create_task(self._send_loop()),
                     asyncio.create_task(self._recv_loop())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (asyncio.IncompleteReadError,
                                                ConnectionError)):
                    log.warning("session %s ended: %r", self.peer, exc)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if clip_started:
                self._clip.stop()
            self.files.close()
            self.inp.release_all()
            self.cap.close()

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        transport = self.conn.writer.transport
        while True:
            start = loop.time()
            if transport.get_write_buffer_size() < BACKPRESSURE_BYTES:
                force, self._force = self._force, False
                res = await self._grab(force)
                if res:
                    await self._send_frame(res)
            await asyncio.sleep(max(0.001, 1.0 / self.fps - (loop.time() - start)))

    async def _recv_loop(self) -> None:
        while True:
            t, body = await asyncio.wait_for(self.conn.recv(), IDLE_TIMEOUT)
            try:
                if t in (MsgType.MOUSE_MOVE, MsgType.MOUSE_BUTTON,
                         MsgType.MOUSE_SCROLL, MsgType.KEY):
                    if not self.allow_control:        # view-only session
                        continue
                    if t is MsgType.MOUSE_MOVE:
                        self.inp.move(*unpack_mouse_move(body))
                    elif t is MsgType.MOUSE_BUTTON:
                        self.inp.button(*unpack_mouse_button(body))
                    elif t is MsgType.MOUSE_SCROLL:
                        self.inp.scroll(*unpack_scroll(body))
                    else:
                        self.inp.key(*unpack_key(body))
                elif t is MsgType.FRAME_REQUEST:
                    await self._apply_settings(unpack_json(body))
                elif t is MsgType.PING:
                    await self.conn.send(MsgType.PONG, body)
                elif t is MsgType.CLIPBOARD:
                    if self._clip:
                        self._clip.on_remote(body)
                elif t.name.startswith("FILE_"):
                    if self.allow_files:
                        await self.files.handle(t, body)
                    elif t is MsgType.FILE_OFFER:
                        await self.conn.send(MsgType.FILE_ACCEPT, pack_json(
                            {"id": unpack_json(body).get("id"), "ok": False,
                             "reason": "file transfer is disabled on the host"}))
                elif t is MsgType.BYE:
                    return
            except (struct.error, UnicodeDecodeError, TypeError, ValueError) as e:
                raise ProtocolError(f"bad {t.name} body") from e

    async def _apply_settings(self, s: dict) -> None:
        if "quality" in s:
            self.quality = _clamp(int(s["quality"]), 10, 95)
        if "fps" in s:
            self.fps = _clamp(int(s["fps"]), 1, 60)
        if "scale" in s:
            self.scale = _clamp(float(s["scale"]), 0.25, 1.0)
        if "monitor" in s:
            await asyncio.get_running_loop().run_in_executor(
                self.cap.executor, self.cap.select, int(s["monitor"]))
            self.inp.set_monitor(self.cap.monitor)
            self._force = True
            await self._send_info()
        if s.get("keyframe"):
            self._force = True


class Host:
    def __init__(self, cfg: Config, capture_factory=None, input_factory=None,
                 services: Services | None = None):
        self.cfg = cfg
        self.svc = services or Services(dest_dir=cfg.dest_dir())
        if capture_factory is None:
            from server.screen_capture import ScreenCapture
            capture_factory = lambda: ScreenCapture(cfg.monitor)
        if input_factory is None:
            from server.input_receiver import InputReceiver
            input_factory = InputReceiver
        self._cf, self._if = capture_factory, input_factory
        self._sessions: set[Session] = set()
        self._fails: dict[str, list[float]] = {}
        self._auth_slots = asyncio.Semaphore(4)   # scrypt is memory-hungry
        self.server: asyncio.base_events.Server | None = None
        self._disc = None
        self.otp = OtpStore(cfg.otp_ttl_minutes * 60, cfg.otp_length)
        self.relay: RelayHostLink | None = None
        self.relay_state = "off"

    # ------------------------------------------------------------ lifecycle
    def _note(self, msg: str) -> None:
        log.info(msg)
        self.svc.notify(msg)

    def _emit_sessions(self) -> None:
        self.svc.sessions_changed([s.peer for s in self._sessions])

    async def start(self):
        self.server = await asyncio.start_server(
            self._on_client, self.cfg.bind, self.cfg.port)
        port = self.server.sockets[0].getsockname()[1]
        if self.cfg.discoverable:
            try:
                self._disc = await discovery.start_responder(
                    self.cfg.machine_id, self.cfg.device_name, port,
                    self.cfg.discovery_port, self.cfg.bind)
            except OSError as e:
                self._note(f"LAN discovery unavailable: {e}")
        self.start_relay()
        return self.server

    # ------------------------------------------------------------------ relay
    def start_relay(self) -> None:
        """(Re)connect to the relay according to the current config."""
        rs = RelaySettings(self.cfg.relay_server.strip(), self.cfg.relay_port,
                           self.cfg.relay_token, self.cfg.relay_tls)
        old, self.relay = self.relay, None
        if old:
            asyncio.ensure_future(old.stop())
        if not rs.enabled:
            self._set_relay_state("off")
            return
        self.relay = RelayHostLink(
            rs, self.cfg.machine_id, identity_from_seed(self.cfg.relay_private),
            self._on_relay_stream, self._set_relay_state, self._new_id)
        self.relay.start()

    def _set_relay_state(self, state: str) -> None:
        self.relay_state = state
        self.svc.relay_status(state)

    def _new_id(self) -> str:
        from core import security
        self.cfg.machine_id = security.generate_machine_id()
        self.cfg.save()
        self._note(f"The ID was taken on the relay; new ID: {self.cfg.machine_id}")
        return self.cfg.machine_id

    async def _on_relay_stream(self, reader, writer, peer: str) -> None:
        await self._serve(reader, writer, peer, "relay")

    async def run(self) -> None:
        server = await self.start()
        async with server:
            await server.serve_forever()

    async def stop(self) -> None:
        if self.relay:
            await self.relay.stop()
            self.relay = None
        self.otp.revoke_all()
        if self._disc:
            self._disc.close()
            self._disc = None
        if self.server:
            self.server.close()
        self.kick_all()
        if self.server:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 3)
            except asyncio.TimeoutError:
                pass
            self.server = None

    def kick_all(self) -> None:
        for s in list(self._sessions):
            s.kick()

    async def send_file(self, path) -> None:
        if not self.cfg.allow_files:
            self.svc.notify("File transfer is disabled in the settings")
            return
        sessions = list(self._sessions)
        if not sessions:
            self.svc.notify("No viewer connected")
        for s in sessions:
            await s.files.send_file(path)

    # ----------------------------------------------------------- connections
    def _blocked(self, ip: str) -> bool:
        now = time.monotonic()
        fails = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
        self._fails[ip] = fails
        return len(fails) >= MAX_FAILS

    async def _approved(self, peer: str) -> bool:
        if not (self.cfg.confirm_connections and self.svc.ask):
            return True
        try:
            return await asyncio.wait_for(self.svc.ask(
                "Incoming connection",
                f"Allow {peer} to view and control this computer?"), 30)
        except asyncio.TimeoutError:
            return False

    def _credentials(self) -> list[tuple[str, str]]:
        creds: list[tuple[str, str]] = []
        if self.cfg.permanent_enabled and self.cfg.password:
            creds.append(("permanent", self.cfg.password))
        creds += [("otp", pw) for pw in self.otp.active()]
        return creds

    async def _on_client(self, reader, writer) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        await self._serve(reader, writer, peer, "lan")

    async def _serve(self, reader, writer, peer: str, via: str) -> None:
        conn = Connection(reader, writer)
        try:
            if self._blocked(peer):
                self._note(f"Blocked {peer}: too many failed attempts")
                return
            creds = self._credentials()
            try:
                async with self._auth_slots:
                    idx = await asyncio.wait_for(
                        host_handshake(conn, [pw for _, pw in creds]), 25)
            except (AuthError, ProtocolError) as e:
                self._fails.setdefault(peer, []).append(time.monotonic())
                self.otp.note_failure()
                self._note(f"Authentication failed from {peer}")
                log.debug("auth detail: %s", e)
                return
            except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                    ConnectionError, OSError):
                return
            kind, secret = creds[idx]
            if kind == "otp" and not self.otp.consume(secret):
                await conn.send(MsgType.ERROR, pack_json({"reason": "One-time password already used"}))
                return
            if len(self._sessions) >= self.cfg.max_sessions:
                await conn.send(MsgType.ERROR, pack_json({"reason": "Host is busy"}))
                return
            if not await self._approved(peer):
                self._note(f"Connection from {peer} denied")
                await conn.send(MsgType.ERROR,
                                pack_json({"reason": "The host denied the connection"}))
                return
            self._fails.pop(peer, None)
            session = Session(conn, self.cfg, peer, self.svc, self._cf, self._if,
                              kind, via)
            self._sessions.add(session)
            self._emit_sessions()
            label = "one-time password" if kind == "otp" else "fixed password"
            self._note(f"Viewer connected: {peer} via {via} ({label})")
            try:
                await session.run()
            finally:
                self._sessions.discard(session)
                self._emit_sessions()
                self._note(f"Viewer disconnected: {peer}")
        except Exception:
            log.exception("unexpected error for %s", peer)
        finally:
            await conn.close()

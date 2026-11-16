"""Consent-based file transfer, usable in both directions.

OFFER -> ACCEPT(ok) -> CHUNK* -> DONE(sha256) -> DONE(ok) ack.
The receiver always decides (GUI prompt); files land in a download folder
under a sanitised, non-overwriting name and are verified with SHA-256.
Transfer ids are namespaced by parity (host even, viewer odd) so an id
identifies a transfer unambiguously on either side.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import struct
import time
from pathlib import Path

from core.protocol import MsgType, ProtocolError, pack_json, unpack_json
from core.services import Services

log = logging.getLogger("remotelink.files")
CHUNK = 256 * 1024
_ID = struct.Struct("!I")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    base = _BAD.sub("_", base).strip(" .")[:200] or "file"
    if base.split(".")[0].upper() in _RESERVED:
        base = "_" + base
    return base


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    i = 1
    while p.exists() or p.with_name(p.name + ".part").exists():
        p = folder / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    return p


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


class _Rx:
    def __init__(self, path: Path, part: Path, fh, size: int):
        self.path, self.part, self.fh, self.size = path, part, fh, size
        self.hasher = hashlib.sha256()
        self.got = 0


class _Tx:
    def __init__(self):
        loop = asyncio.get_running_loop()
        self.accepted: asyncio.Future = loop.create_future()
        self.ack = asyncio.Event()
        self.result = False
        self.aborted = False


class FileTransfer:
    def __init__(self, conn, services: Services, id_parity: int):
        self.conn, self.svc, self._parity = conn, services, id_parity
        self._seq = 0
        self._rx: dict[int, _Rx] = {}
        self._tx: dict[int, _Tx] = {}
        self._tx_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def _new_id(self) -> int:
        self._seq += 1
        return self._seq * 2 + self._parity

    # ------------------------------------------------------------ receiving
    async def handle(self, t: MsgType, body: bytes) -> None:
        if t is MsgType.FILE_OFFER:
            task = asyncio.create_task(self._on_offer(unpack_json(body)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)   # never block the recv loop
        elif t is MsgType.FILE_ACCEPT:
            m = unpack_json(body)
            tx = self._tx.get(m.get("id"))
            if tx and not tx.accepted.done():
                tx.accepted.set_result((bool(m.get("ok")), str(m.get("reason", ""))))
        elif t is MsgType.FILE_CHUNK:
            await self._on_chunk(body)
        elif t is MsgType.FILE_DONE:
            await self._on_done(unpack_json(body))
        elif t is MsgType.FILE_ABORT:
            self._on_abort(unpack_json(body))

    async def _reply(self, fid: int, ok: bool, reason: str = "") -> None:
        await self.conn.send(MsgType.FILE_ACCEPT,
                             pack_json({"id": fid, "ok": ok, "reason": reason}))

    async def _on_offer(self, m: dict) -> None:
        try:
            fid, size = int(m["id"]), int(m["size"])
            name = safe_name(str(m["name"]))
            if size < 0 or fid in self._rx:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            return
        try:
            reason = ""
            if size > self.svc.max_file_bytes:
                reason = "file too large"
            elif self.svc.ask:
                try:
                    ok = await asyncio.wait_for(self.svc.ask(
                        "Incoming file", f'Accept "{name}" ({fmt_size(size)})?'), 60)
                except asyncio.TimeoutError:
                    ok = False
                if not ok:
                    reason = "declined"
            if reason:
                await self._reply(fid, False, reason)
                return
            try:
                self.svc.dest_dir.mkdir(parents=True, exist_ok=True)
                path = unique_path(self.svc.dest_dir, name)
                part = path.with_name(path.name + ".part")
                rx = _Rx(path, part, open(part, "wb"), size)
            except OSError as e:
                await self._reply(fid, False, str(e))
                return
            self._rx[fid] = rx
            await self._reply(fid, True)
            self.svc.notify(f"Receiving {name} ({fmt_size(size)})...")
        except Exception as e:
            log.debug("offer handling failed: %r", e)

    async def _on_chunk(self, body: bytes) -> None:
        if len(body) < _ID.size:
            raise ProtocolError("short chunk")
        (fid,) = _ID.unpack_from(body)
        rx = self._rx.get(fid)
        if rx is None:
            return
        data = body[_ID.size:]
        rx.got += len(data)
        if rx.got > rx.size:
            self._drop(fid)
            await self.conn.send(MsgType.FILE_ABORT,
                                 pack_json({"id": fid, "reason": "too much data"}))
            return
        rx.fh.write(data)
        rx.hasher.update(data)

    async def _on_done(self, m: dict) -> None:
        fid = m.get("id")
        if fid in self._rx:
            rx = self._rx.pop(fid)
            rx.fh.close()
            ok = rx.got == rx.size and hmac.compare_digest(
                rx.hasher.hexdigest(), str(m.get("sha256", "")))
            if ok:
                os.replace(rx.part, rx.path)
                self.svc.notify(f"Received {rx.path.name} -> {rx.path.parent}")
            else:
                rx.part.unlink(missing_ok=True)
                self.svc.notify(f"{rx.path.name}: integrity check failed, discarded")
            await self.conn.send(MsgType.FILE_DONE, pack_json({"id": fid, "ok": ok}))
        elif fid in self._tx:
            tx = self._tx[fid]
            tx.result = bool(m.get("ok"))
            tx.ack.set()

    def _on_abort(self, m: dict) -> None:
        fid = m.get("id")
        if fid in self._rx:
            self._drop(fid)
            self.svc.notify("Transfer cancelled by the other side")
        tx = self._tx.get(fid)
        if tx:
            tx.aborted = True
            if not tx.accepted.done():
                tx.accepted.set_result((False, str(m.get("reason", "aborted"))))
            tx.ack.set()

    def _drop(self, fid: int) -> None:
        rx = self._rx.pop(fid, None)
        if rx:
            try:
                rx.fh.close()
                rx.part.unlink(missing_ok=True)
            except OSError:
                pass

    # -------------------------------------------------------------- sending
    async def send_file(self, path) -> bool:
        path = Path(path)
        try:
            size = path.stat().st_size
            fh = open(path, "rb")
        except OSError as e:
            self.svc.notify(f"Cannot read {path.name}: {e}")
            return False
        notify = self.svc.notify
        async with self._tx_lock:
            fid = self._new_id()
            tx = self._tx[fid] = _Tx()
            try:
                await self.conn.send(MsgType.FILE_OFFER, pack_json(
                    {"id": fid, "name": path.name, "size": size}))
                notify(f"Waiting for the other side to accept {path.name}...")
                ok, reason = await asyncio.wait_for(tx.accepted, 120)
                if not ok:
                    notify(f"{path.name} was not accepted ({reason})")
                    return False
                digest, sent, last = hashlib.sha256(), 0, time.monotonic()
                while True:
                    chunk = await asyncio.to_thread(fh.read, CHUNK)
                    if not chunk:
                        break
                    if tx.aborted:
                        notify(f"{path.name}: cancelled")
                        return False
                    digest.update(chunk)
                    await self.conn.send(MsgType.FILE_CHUNK, _ID.pack(fid) + chunk)
                    sent += len(chunk)
                    if time.monotonic() - last > 1:
                        notify(f"Sending {path.name}: {sent * 100 // max(size, 1)}%")
                        last = time.monotonic()
                await self.conn.send(MsgType.FILE_DONE, pack_json(
                    {"id": fid, "sha256": digest.hexdigest()}))
                await asyncio.wait_for(tx.ack.wait(), 120)
                notify(f"{path.name} sent" if tx.result
                       else f"{path.name}: transfer failed")
                return tx.result
            except asyncio.TimeoutError:
                notify(f"{path.name}: timed out")
                return False
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                notify(f"{path.name}: connection lost")
                return False
            finally:
                fh.close()
                self._tx.pop(fid, None)

    def close(self) -> None:
        for fid in list(self._rx):
            self._drop(fid)
        for tx in self._tx.values():
            tx.aborted = True
            if not tx.accepted.done():
                tx.accepted.set_result((False, "disconnected"))
            tx.ack.set()
        for t in list(self._tasks):
            t.cancel()

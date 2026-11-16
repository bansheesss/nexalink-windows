"""Client side of the relay: host registration link + viewer connect."""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
from dataclasses import dataclass
from typing import Awaitable, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from core.network import Connection
from core.relay_proto import (DEFAULT_RELAY_PORT, RELAY_VERSION, RelayError,
                              challenge_message, read_msg, write_msg)

log = logging.getLogger("remotelink.relay.client")


@dataclass
class RelaySettings:
    server: str
    port: int = DEFAULT_RELAY_PORT
    token: str = ""
    tls: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.server.strip())


def identity_from_seed(seed_hex: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))


def new_identity_seed() -> str:
    return os.urandom(32).hex()


def public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


async def _open(rs: RelaySettings, timeout: float = 8.0):
    ctx = ssl.create_default_context() if rs.tls else None
    return await asyncio.wait_for(asyncio.open_connection(
        rs.server, rs.port, ssl=ctx,
        server_hostname=rs.server if rs.tls else None), timeout)


async def connect_via_relay(rs: RelaySettings, target_id: str,
                            timeout: float = 20.0) -> Connection:
    """Viewer side: returns a Connection already paired with the host.
    The caller runs client_handshake() on it as for a direct connection."""
    r, w = await _open(rs)
    try:
        await write_msg(w, {"t": "connect", "v": RELAY_VERSION, "id": target_id,
                            "token": rs.token})
        resp = await read_msg(r, timeout)
        if resp.get("t") != "ready":
            raise RelayError(str(resp.get("code", "error")))
        return Connection(r, w)
    except BaseException:
        w.close()
        raise


class RelayHostLink:
    """Keeps this machine registered with the relay and hands incoming
    sessions to `on_stream(reader, writer, peer_ip)`."""

    def __init__(self, rs: RelaySettings, machine_id: str, key: Ed25519PrivateKey,
                 on_stream: Callable[[asyncio.StreamReader, asyncio.StreamWriter, str], Awaitable[None]],
                 status: Callable[[str], None] = lambda s: None,
                 on_id_taken: Callable[[], str] | None = None):
        self.rs, self.id, self.key = rs, machine_id, key
        self.on_stream, self.status, self.on_id_taken = on_stream, status, on_id_taken
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self.online = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for t in list(self._tasks):
            t.cancel()
        self.online = False

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                self.status("connecting")
                await self._session()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except RelayError as e:
                self.status(f"error:{e.code}")
                if e.code == "id_taken" and self.on_id_taken:
                    self.id = self.on_id_taken()
                    continue
                if e.code in ("bad_token", "auth"):
                    delay = 30.0
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ssl.SSLError) as e:
                log.debug("relay link error: %r", e)
                self.status("offline")
            self.online = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _session(self) -> None:
        r, w = await _open(self.rs)
        ping = None
        try:
            await write_msg(w, {"t": "register", "v": RELAY_VERSION, "id": self.id,
                                "pub": public_hex(self.key), "token": self.rs.token})
            ch = await read_msg(r, 10)
            if ch.get("t") == "error":
                raise RelayError(str(ch.get("code")))
            sig = self.key.sign(challenge_message(bytes.fromhex(ch["nonce"]), self.id))
            await write_msg(w, {"t": "auth", "sig": sig.hex()})
            ok = await read_msg(r, 10)
            if ok.get("t") != "registered":
                raise RelayError(str(ok.get("code", "error")))
            self.online = True
            self.status("online")

            async def pinger():
                while True:
                    await asyncio.sleep(15)
                    await write_msg(w, {"t": "ping"})
            ping = asyncio.create_task(pinger())
            while True:
                msg = await read_msg(r, 45)
                if msg.get("t") == "incoming":
                    t = asyncio.create_task(self._accept(str(msg.get("sid", "")),
                                                         str(msg.get("peer", "relay"))))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
        finally:
            if ping:
                ping.cancel()
            self.online = False
            w.close()

    async def _accept(self, sid: str, peer: str) -> None:
        try:
            r, w = await _open(self.rs)
            try:
                await write_msg(w, {"t": "accept", "v": RELAY_VERSION, "sid": sid,
                                    "token": self.rs.token})
                resp = await read_msg(r, 10)
                if resp.get("t") != "ready":
                    w.close()
                    return
            except BaseException:
                w.close()
                raise
            await self.on_stream(r, w, peer)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, RelayError):
            pass

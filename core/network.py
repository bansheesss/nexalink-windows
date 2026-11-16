"""Framed, encrypted asyncio connection + authentication handshake."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import struct

from . import security
from .protocol import (MAX_FRAME_SIZE, MAX_HANDSHAKE_SIZE, PROTOCOL_VERSION,
                       MsgType, ProtocolError)
from .security import AuthError, SessionCipher

_LEN = struct.Struct("!I")
TAG_LEN = 16


def _tune_socket(writer: asyncio.StreamWriter) -> None:
    """Low latency: disable Nagle; detect dead peers with keepalive."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.cipher: SessionCipher | None = None
        self._send_lock = asyncio.Lock()
        _tune_socket(writer)

    @classmethod
    async def open(cls, host: str, port: int, timeout: float = 5.0) -> "Connection":
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        return cls(r, w)

    # --- plaintext frames (handshake only) ---
    async def write_plain(self, payload: bytes) -> None:
        async with self._send_lock:
            self.writer.write(_LEN.pack(len(payload)) + payload)
            await self.writer.drain()

    async def read_plain(self) -> bytes:
        (n,) = _LEN.unpack(await self.reader.readexactly(4))
        if n > MAX_HANDSHAKE_SIZE:
            raise ProtocolError("handshake frame too large")
        return await self.reader.readexactly(n)

    # --- encrypted frames ---
    async def send(self, msg_type: MsgType, body: bytes = b"") -> None:
        if self.cipher is None:
            raise ProtocolError("not authenticated")
        plain = bytes([msg_type]) + body
        hdr = _LEN.pack(len(plain) + TAG_LEN)
        async with self._send_lock:   # lock covers encrypt so counter order == wire order
            self.writer.write(hdr + self.cipher.encrypt(plain, hdr))
            await self.writer.drain()

    async def recv(self) -> tuple[MsgType, bytes]:
        if self.cipher is None:
            raise ProtocolError("not authenticated")
        hdr = await self.reader.readexactly(4)
        (n,) = _LEN.unpack(hdr)
        if not TAG_LEN < n <= MAX_FRAME_SIZE:
            raise ProtocolError("bad frame length")
        plain = self.cipher.decrypt(await self.reader.readexactly(n), hdr)
        try:
            return MsgType(plain[0]), plain[1:]
        except ValueError as e:
            raise ProtocolError("unknown message type") from e

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


def _parse(raw: bytes) -> dict:
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError
        return obj
    except ValueError as e:
        raise ProtocolError("malformed handshake") from e


def _dump(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


async def client_handshake(conn: Connection, password: str) -> None:
    eph = security.new_ephemeral()
    hello = _dump({"v": PROTOCOL_VERSION, "pub": security.public_bytes(eph).hex()})
    await conn.write_plain(hello)
    ack_raw = await conn.read_plain()
    ack = _parse(ack_raw)
    try:
        if ack["v"] != PROTOCOL_VERSION:
            raise ProtocolError("protocol version mismatch")
        host_pub, salt = bytes.fromhex(ack["pub"]), bytes.fromhex(ack["salt"])
    except (KeyError, ValueError) as e:
        raise ProtocolError("malformed handshake") from e
    transcript = hashlib.sha256(hello + ack_raw).digest()
    keys = await asyncio.to_thread(security.derive_session, eph, host_pub,
                                   password, salt, transcript, True)
    await conn.write_plain(_dump({"proof": keys.proof(b"client").hex()}))
    resp = _parse(await conn.read_plain())
    if not resp.get("ok"):
        raise AuthError("wrong password or ID")
    try:
        host_proof = bytes.fromhex(resp["proof"])
    except (KeyError, ValueError) as e:
        raise ProtocolError("malformed handshake") from e
    if not hmac.compare_digest(host_proof, keys.proof(b"host")):
        raise AuthError("host failed to prove knowledge of password")
    conn.cipher = keys.cipher


async def host_handshake(conn: Connection, password) -> int:
    """Authenticate a viewer. `password` is one string or a list of acceptable
    passwords (permanent + one-time ones). The wire format is unchanged: the
    host derives keys for every candidate and accepts the one whose proof
    matches. Returns the index of the matching password."""
    candidates = [password] if isinstance(password, str) else list(password)
    hello_raw = await conn.read_plain()
    hello = _parse(hello_raw)
    try:
        if hello["v"] != PROTOCOL_VERSION:
            raise ProtocolError("protocol version mismatch")
        client_pub = bytes.fromhex(hello["pub"])
    except (KeyError, ValueError) as e:
        raise ProtocolError("malformed handshake") from e
    eph, salt = security.new_ephemeral(), os.urandom(16)
    ack = _dump({"v": PROTOCOL_VERSION, "pub": security.public_bytes(eph).hex(),
                 "salt": salt.hex()})
    await conn.write_plain(ack)
    transcript = hashlib.sha256(hello_raw + ack).digest()
    keysets = await asyncio.gather(*[
        asyncio.to_thread(security.derive_session, eph, client_pub,
                          pw, salt, transcript, False) for pw in candidates])
    msg = _parse(await conn.read_plain())
    try:
        client_proof = bytes.fromhex(msg["proof"])
    except (KeyError, ValueError) as e:
        raise ProtocolError("malformed handshake") from e
    matched = None
    for i, keys in enumerate(keysets):            # no early exit: constant work
        if hmac.compare_digest(client_proof, keys.proof(b"client")) and matched is None:
            matched = i
    if matched is None:
        await conn.write_plain(_dump({"ok": False}))
        raise AuthError("client authentication failed")
    keys = keysets[matched]
    await conn.write_plain(_dump({"ok": True, "proof": keys.proof(b"host").hex()}))
    conn.cipher = keys.cipher
    return matched

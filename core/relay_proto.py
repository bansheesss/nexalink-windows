"""Relay control protocol (rendezvous + byte relay), shared by server and clients.

Control frames: [u32 BE length][JSON], max 16 KiB, same framing style as the
RemoteLink handshake. After a session is paired ("ready" sent to both sides)
the relay only copies raw bytes; the normal end-to-end encrypted RemoteLink
protocol runs on top, so the relay never sees screen data or passwords.

  host   -> {"t":"register","v":1,"id","pub","token"}  <- {"t":"challenge","nonce"}
         -> {"t":"auth","sig"}                          <- {"t":"registered"}
         -> {"t":"ping"} every 15 s                     <- {"t":"pong"}
         <- {"t":"incoming","sid","peer"}   (a viewer wants in)
  host   -> (new TCP) {"t":"accept","v":1,"sid","token"}  <- {"t":"ready"}
  viewer -> {"t":"connect","v":1,"id","token"}           <- {"t":"ready"} | error
Errors: {"t":"error","code":...} with code in bad_request, bad_token,
id_taken, auth, offline, unavailable, rate_limited, busy.
"""
from __future__ import annotations

import asyncio
import json
import re
import struct

RELAY_VERSION = 1
DEFAULT_RELAY_PORT = 7072
MAX_CTRL = 16 * 1024
_ID_RE = re.compile(r"\d{9}")


class RelayError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def valid_id(s) -> bool:
    return isinstance(s, str) and bool(_ID_RE.fullmatch(s))


def challenge_message(nonce: bytes, machine_id: str) -> bytes:
    return b"remotelink-relay-v1|" + nonce + b"|" + machine_id.encode("ascii")


def encode(obj: dict) -> bytes:
    payload = json.dumps(obj, separators=(",", ":")).encode()
    return struct.pack("!I", len(payload)) + payload


async def write_msg(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write(encode(obj))
    await writer.drain()


async def read_msg(reader: asyncio.StreamReader, timeout: float | None = 10.0) -> dict:
    async def _read():
        (n,) = struct.unpack("!I", await reader.readexactly(4))
        if n > MAX_CTRL:
            raise RelayError("bad_request", "control frame too large")
        return await reader.readexactly(n)
    raw = await (asyncio.wait_for(_read(), timeout) if timeout else _read())
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise RelayError("bad_request", "bad json") from e
    if not isinstance(obj, dict):
        raise RelayError("bad_request", "json must be an object")
    return obj

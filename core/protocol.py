"""RemoteLink wire protocol: message types and (de)serialisation helpers.

Wire frame:   [length: u32 BE][ciphertext || 16-byte GCM tag]
Plaintext:    [msg_type: u8][body ...]
Hot-path messages (mouse, frames) use struct; rare ones use JSON.
"""
from __future__ import annotations

import enum
import json
import struct
from dataclasses import dataclass

PROTOCOL_VERSION = 1
MAX_HANDSHAKE_SIZE = 64 * 1024            # pre-auth frames stay tiny
MAX_FRAME_SIZE = 32 * 1024 * 1024         # post-auth upper bound


class ProtocolError(Exception):
    pass


class MsgType(enum.IntEnum):
    # session
    PING = 0x01
    PONG = 0x02
    BYE = 0x03
    ERROR = 0x04
    SESSION_INFO = 0x05      # JSON: screen size, monitors, capabilities
    # video
    FRAME = 0x10
    FRAME_REQUEST = 0x11     # viewer asks for settings change / keyframe
    # input (viewer -> host)
    MOUSE_MOVE = 0x20
    MOUSE_BUTTON = 0x21
    MOUSE_SCROLL = 0x22
    KEY = 0x23
    # clipboard
    CLIPBOARD = 0x30
    # file transfer
    FILE_OFFER = 0x40
    FILE_ACCEPT = 0x41
    FILE_CHUNK = 0x42
    FILE_DONE = 0x43
    FILE_ABORT = 0x44


class Codec(enum.IntEnum):
    JPEG = 1
    WEBP = 2


# ---- video frame -------------------------------------------------------
_FRAME_HDR = struct.Struct("!IIBHH")  # frame_id, ts_ms, codec, width, height


@dataclass
class Frame:
    frame_id: int
    timestamp_ms: int
    codec: Codec
    width: int
    height: int
    data: bytes

    def pack(self) -> bytes:
        return _FRAME_HDR.pack(self.frame_id, self.timestamp_ms & 0xFFFFFFFF,
                               int(self.codec), self.width, self.height) + self.data

    @classmethod
    def unpack(cls, body: bytes) -> "Frame":
        if len(body) < _FRAME_HDR.size:
            raise ProtocolError("short frame")
        fid, ts, codec, w, h = _FRAME_HDR.unpack_from(body)
        return cls(fid, ts, Codec(codec), w, h, body[_FRAME_HDR.size:])


# ---- input -------------------------------------------------------------
# Mouse coordinates are normalised to 0..65535 so that resolution and
# DPI differences between viewer and host never matter.
_MOVE = struct.Struct("!HH")
_BTN = struct.Struct("!BB")     # button (1=left,2=right,3=middle), pressed
_SCROLL = struct.Struct("!hh")  # dx, dy


def pack_mouse_move(nx: float, ny: float) -> bytes:
    cl = lambda v: max(0, min(65535, int(v * 65535)))
    return _MOVE.pack(cl(nx), cl(ny))


def unpack_mouse_move(body: bytes) -> tuple[float, float]:
    x, y = _MOVE.unpack(body)
    return x / 65535, y / 65535


def pack_mouse_button(button: int, pressed: bool) -> bytes:
    return _BTN.pack(button, int(pressed))


def unpack_mouse_button(body: bytes) -> tuple[int, bool]:
    b, p = _BTN.unpack(body)
    return b, bool(p)


def pack_scroll(dx: int, dy: int) -> bytes:
    return _SCROLL.pack(dx, dy)


def unpack_scroll(body: bytes) -> tuple[int, int]:
    return _SCROLL.unpack(body)


def pack_key(name: str, pressed: bool) -> bytes:
    """Keys travel as platform-neutral names ('a', 'enter', 'ctrl_l', ...)."""
    return bytes([int(pressed)]) + name.encode("utf-8")


def unpack_key(body: bytes) -> tuple[str, bool]:
    return body[1:].decode("utf-8"), bool(body[0])


# ---- JSON helpers ------------------------------------------------------
def pack_json(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def unpack_json(body: bytes) -> dict:
    try:
        obj = json.loads(body)
    except ValueError as e:
        raise ProtocolError("bad json") from e
    if not isinstance(obj, dict):
        raise ProtocolError("json must be an object")
    return obj

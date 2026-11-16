"""Password gate for the settings screen (a UI lock, not file encryption)."""
from __future__ import annotations

import hashlib
import hmac
import os
import time

_N, _R, _P = 2 ** 14, 8, 1


def make_hash(password: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt1${salt.hex()}${h.hex()}"


def verify(stored: str, password: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt1":
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                           n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(h.hex(), digest)
    except (ValueError, TypeError):
        return False


class Throttle:
    """Exponential back-off after wrong attempts (2^n seconds, max 60)."""

    def __init__(self, clock=time.monotonic):
        self._clock, self._fails, self._until = clock, 0, 0.0

    def wait_seconds(self) -> float:
        return max(0.0, self._until - self._clock())

    def failed(self) -> None:
        self._fails += 1
        self._until = self._clock() + min(60.0, 2.0 ** (self._fails - 1))

    def ok(self) -> None:
        self._fails, self._until = 0, 0.0

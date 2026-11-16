"""One-time passwords: valid for one successful login, expire after a TTL."""
from __future__ import annotations

import time
from typing import Callable

from core import security


class OtpStore:
    MAX_ACTIVE = 3

    def __init__(self, ttl_seconds: float = 600, length: int = 8,
                 max_failures: int = 10, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.length, self.max_failures = ttl_seconds, length, max_failures
        self._clock = clock
        self._items: dict[str, float] = {}
        self._fails = 0

    def _prune(self) -> None:
        now = self._clock()
        for pw in [p for p, exp in self._items.items() if exp <= now]:
            del self._items[pw]

    def create(self) -> str:
        self._prune()
        while len(self._items) >= self.MAX_ACTIVE:           # drop the oldest
            del self._items[min(self._items, key=self._items.get)]
        pw = security.generate_password(self.length)
        self._items[pw] = self._clock() + self.ttl
        self._fails = 0
        return pw

    def active(self) -> list[str]:
        self._prune()
        return list(self._items)

    def remaining(self, pw: str) -> float:
        self._prune()
        return max(0.0, self._items.get(pw, 0) - self._clock())

    def consume(self, pw: str) -> bool:
        """Atomically use up an OTP; False if it is unknown, expired or used."""
        self._prune()
        return self._items.pop(pw, None) is not None

    def note_failure(self) -> None:
        """Too many wrong guesses while an OTP is live revoke all of them."""
        if not self._items:
            return
        self._fails += 1
        if self._fails >= self.max_failures:
            self._items.clear()
            self._fails = 0

    def revoke_all(self) -> None:
        self._items.clear()

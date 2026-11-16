"""Open a connection to a target: LAN first (by ID), then the relay."""
from __future__ import annotations

import asyncio
import re

from core import discovery
from core.network import Connection
from core.relay_client import RelaySettings, connect_via_relay
from core.relay_proto import RelayError

_ID_RE = re.compile(r"\d{3}\s?\d{3}\s?\d{3}")

_RELAY_MESSAGES = {
    "offline": "دستگاه مقصد آنلاین نیست (یا به ریلی وصل نشده).",
    "unavailable": "دستگاه مقصد پاسخ نداد.",
    "bad_token": "توکن ریلی اشتباه است.",
    "rate_limited": "تعداد تلاش‌ها زیاد است؛ کمی بعد دوباره امتحان کنید.",
    "busy": "سرور ریلی مشغول است.",
}


class ConnectError(Exception):
    pass


def is_id(target: str) -> bool:
    return bool(_ID_RE.fullmatch(target.strip()))


async def open_target(target: str, port: int, relay: RelaySettings | None,
                      lan_timeout: float = 1.2) -> tuple[Connection, str]:
    """Returns (connection, mode) with mode in {"direct", "lan", "relay"}."""
    target = target.strip()
    if not is_id(target):
        try:
            return await Connection.open(target, port), "direct"
        except OSError as e:
            raise ConnectError(f"اتصال برقرار نشد: {e}") from e
    mid = re.sub(r"\s", "", target)
    found = await asyncio.to_thread(discovery.discover, mid, lan_timeout)
    if found:
        h = found[0]
        try:
            return await Connection.open(h["host"], h["port"]), "lan"
        except OSError:
            pass                                    # fall through to the relay
    if relay and relay.enabled:
        try:
            return await connect_via_relay(relay, mid), "relay"
        except RelayError as e:
            raise ConnectError(_RELAY_MESSAGES.get(e.code, f"خطای ریلی: {e.code}")) from e
        except (OSError, asyncio.TimeoutError) as e:
            raise ConnectError(f"اتصال به سرور ریلی ممکن نیست: {e}") from e
    raise ConnectError("دستگاهی با این شناسه در شبکه‌ی محلی پیدا نشد و سرور ریلی تنظیم نشده است.")

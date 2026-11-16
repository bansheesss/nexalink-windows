"""LAN discovery over UDP broadcast: find a host by its 9-digit ID.

Query:  b"RLK1?" + (id | "*")        Reply: b"RLK1!" + json{id,name,port}
Hosts answer only if `discoverable` is on. Works on the local network only;
reaching a host over the internet needs a rendezvous/relay server (future).
"""
from __future__ import annotations

import asyncio
import json
import socket
import time

DISCOVERY_PORT = 7071
MAGIC = b"RLK1"


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, info: dict):
        self.info = info
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if not data.startswith(MAGIC + b"?") or self.transport is None:
            return
        want = data[len(MAGIC) + 1:].decode("ascii", "ignore").strip()
        if want in ("", "*", self.info["id"]):
            self.transport.sendto(MAGIC + b"!" + json.dumps(self.info).encode(), addr)


async def start_responder(machine_id: str, name: str, tcp_port: int,
                          discovery_port: int = DISCOVERY_PORT,
                          bind: str = "0.0.0.0") -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Responder({"id": machine_id, "name": name, "port": tcp_port}),
        local_addr=(bind, discovery_port))
    return transport


def _broadcast_targets() -> list[str]:
    from utils.helpers import local_ips
    targets = ["255.255.255.255"]
    for ip in local_ips():
        b = ip.rsplit(".", 1)[0] + ".255"       # assumes a /24, the common case
        if b not in targets:
            targets.append(b)
    return targets


def discover(machine_id: str | None = None, timeout: float = 1.5,
             port: int = DISCOVERY_PORT, targets: list[str] | None = None) -> list[dict]:
    """Blocking. Returns [{"id","name","port","host"}] for hosts that answered."""
    query = MAGIC + b"?" + (machine_id or "*").encode("ascii")
    targets = targets or _broadcast_targets()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.2)
    found: dict[str, dict] = {}
    start = time.monotonic()
    sends = 0
    try:
        while time.monotonic() - start < timeout:
            if sends < 2 and time.monotonic() - start >= sends * timeout / 2:
                for t in targets:               # resend once: UDP can drop
                    try:
                        s.sendto(query, (t, port))
                    except OSError:
                        pass
                sends += 1
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(MAGIC + b"!"):
                continue
            try:
                info = json.loads(data[len(MAGIC) + 1:])
                info = {"id": str(info["id"]), "name": str(info.get("name", "")),
                        "port": int(info["port"]), "host": addr[0]}
            except (ValueError, KeyError, TypeError):
                continue
            found[info["id"]] = info
            if machine_id:
                break
    finally:
        s.close()
    return list(found.values())

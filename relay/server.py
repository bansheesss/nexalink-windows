"""RemoteLink relay server: lets two clients behind NAT reach each other.

    python -m relay --port 7072 --token SECRET [--cert c.pem --key k.pem]

Hosts keep an outbound control connection (registered by ID, proven with an
Ed25519 key, first-come-first-served). A viewer asks for an ID; the relay tells
the host, the host opens a second outbound connection, and the relay pipes the
two sockets together. Only ciphertext passes through after pairing.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import ssl
import time
from collections import defaultdict, deque
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.relay_proto import (DEFAULT_RELAY_PORT, RELAY_VERSION, RelayError,
                              challenge_message, read_msg, valid_id, write_msg)

log = logging.getLogger("remotelink.relay")


class _Control:
    def __init__(self, machine_id: str, writer: asyncio.StreamWriter):
        self.id, self.writer = machine_id, writer
        self._lock = asyncio.Lock()

    async def send(self, obj: dict) -> None:
        async with self._lock:
            await write_msg(self.writer, obj)

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass


class _Limiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class RelayServer:
    def __init__(self, host="0.0.0.0", port=DEFAULT_RELAY_PORT, token="",
                 registry_path: str | None = None, ssl_ctx=None,
                 pair_timeout=12.0, control_timeout=45.0, idle_timeout=120.0,
                 max_sessions=500, connects_per_min=30, new_ids_per_hour=20,
                 registry_ttl_days=365):
        self.host, self.port, self.token = host, port, token
        self.registry_path = Path(registry_path) if registry_path else None
        self.ssl_ctx = ssl_ctx
        self.pair_timeout, self.control_timeout = pair_timeout, control_timeout
        self.idle_timeout, self.max_sessions = idle_timeout, max_sessions
        self.registry_ttl = registry_ttl_days * 86400
        self.registry: dict[str, dict] = {}
        self.controls: dict[str, _Control] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.sessions = 0
        self._connect_limit = _Limiter(connects_per_min, 60)
        self._newid_limit = _Limiter(new_ids_per_hour, 3600)
        self.server: asyncio.base_events.Server | None = None
        self._load()

    # --------------------------------------------------------------- registry
    def _load(self) -> None:
        if not self.registry_path or not self.registry_path.exists():
            return
        try:
            self.registry = json.loads(self.registry_path.read_text())
        except (OSError, ValueError):
            log.warning("could not read registry, starting empty")
        cutoff = time.time() - self.registry_ttl
        self.registry = {k: v for k, v in self.registry.items()
                         if v.get("seen", 0) >= cutoff}

    def _save(self) -> None:
        if not self.registry_path:
            return
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.registry))
        os.replace(tmp, self.registry_path)

    # ------------------------------------------------------------- lifecycle
    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, self.host, self.port, ssl=self.ssl_ctx)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("relay listening on %s:%s%s%s", self.host, self.port,
                 " (TLS)" if self.ssl_ctx else "",
                 "" if self.token else " [NO TOKEN: anyone can use this relay]")
        return self.server

    async def stop(self) -> None:
        if self.server:
            self.server.close()
        for c in list(self.controls.values()):
            c.close()
        self._save()

    # --------------------------------------------------------------- handlers
    @staticmethod
    def _ip(writer) -> str:
        return (writer.get_extra_info("peername") or ("?",))[0]

    async def _err(self, writer, code: str) -> None:
        try:
            await write_msg(writer, {"t": "error", "code": code})
        except Exception:
            pass

    def _token_ok(self, msg: dict) -> bool:
        if not self.token:
            return True
        return hmac.compare_digest(str(msg.get("token", "")).encode(), self.token.encode())

    async def _handle(self, reader, writer) -> None:
        keep_open = False
        try:
            msg = await read_msg(reader, 10)
            if msg.get("v") != RELAY_VERSION:
                await self._err(writer, "bad_request")
                return
            if not self._token_ok(msg):
                await self._err(writer, "bad_token")
                return
            t = msg.get("t")
            if t == "register":
                await self._control(msg, reader, writer)
            elif t == "connect":
                await self._connect(msg, reader, writer)
            elif t == "accept":
                keep_open = await self._accept(msg, reader, writer)
            else:
                await self._err(writer, "bad_request")
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError,
                RelayError, OSError):
            pass
        except Exception:
            log.exception("relay handler error")
        finally:
            if not keep_open:
                try:
                    writer.close()
                except Exception:
                    pass

    async def _control(self, msg, reader, writer) -> None:
        ip, mid = self._ip(writer), msg.get("id")
        try:
            pub = bytes.fromhex(str(msg.get("pub", "")))
            key = Ed25519PublicKey.from_public_bytes(pub)
        except ValueError:
            await self._err(writer, "bad_request")
            return
        if not valid_id(mid):
            await self._err(writer, "bad_request")
            return
        entry = self.registry.get(mid)
        if entry is None and not self._newid_limit.allow(ip):
            await self._err(writer, "rate_limited")
            return
        if entry is not None and entry["pub"] != pub.hex():
            await self._err(writer, "id_taken")
            return
        nonce = os.urandom(32)
        await write_msg(writer, {"t": "challenge", "nonce": nonce.hex()})
        resp = await read_msg(reader, 10)
        try:
            key.verify(bytes.fromhex(str(resp.get("sig", ""))), challenge_message(nonce, mid))
        except (InvalidSignature, ValueError):
            await self._err(writer, "auth")
            return
        if entry is None:
            self.registry[mid] = entry = {"pub": pub.hex(), "seen": time.time()}
        entry["seen"] = time.time()
        self._save()
        old = self.controls.get(mid)
        ctrl = _Control(mid, writer)
        self.controls[mid] = ctrl
        if old:
            old.close()
        await ctrl.send({"t": "registered"})
        log.info("host %s online (%s)", mid, ip)
        try:
            while True:
                m = await read_msg(reader, self.control_timeout)
                if m.get("t") == "ping":
                    entry["seen"] = time.time()
                    await ctrl.send({"t": "pong"})
                elif m.get("t") == "bye":
                    break
        finally:
            if self.controls.get(mid) is ctrl:
                del self.controls[mid]
            log.info("host %s offline", mid)

    async def _connect(self, msg, reader, writer) -> None:
        ip, target = self._ip(writer), msg.get("id")
        if not valid_id(target):
            await self._err(writer, "bad_request")
            return
        if not self._connect_limit.allow(ip):
            await self._err(writer, "rate_limited")
            return
        ctrl = self.controls.get(target)
        if ctrl is None:
            await self._err(writer, "offline")
            return
        if self.sessions + len(self.pending) >= self.max_sessions:
            await self._err(writer, "busy")
            return
        sid = os.urandom(16).hex()
        fut = asyncio.get_running_loop().create_future()
        self.pending[sid] = fut
        try:
            await ctrl.send({"t": "incoming", "sid": sid, "peer": ip})
            hr, hw = await asyncio.wait_for(fut, self.pair_timeout)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            await self._err(writer, "unavailable")
            return
        finally:
            self.pending.pop(sid, None)
        try:
            await write_msg(hw, {"t": "ready"})
            await write_msg(writer, {"t": "ready"})
            self.sessions += 1
            log.info("session %s -> %s (%s)", ip, target, sid[:6])
            await self._pipe(reader, writer, hr, hw)
        finally:
            self.sessions -= 1
            for w in (hw,):
                try:
                    w.close()
                except Exception:
                    pass

    async def _accept(self, msg, reader, writer) -> bool:
        fut = self.pending.get(str(msg.get("sid", "")))
        if fut is None or fut.done():
            await self._err(writer, "unavailable")
            return False
        fut.set_result((reader, writer))
        return True          # the viewer's handler now owns and closes this socket

    async def _pipe(self, ar, aw, br, bw) -> None:
        async def copy(r, w):
            try:
                while True:
                    data = await asyncio.wait_for(r.read(65536), self.idle_timeout)
                    if not data:
                        return
                    w.write(data)
                    await w.drain()
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return
        t1 = asyncio.create_task(copy(ar, bw))
        t2 = asyncio.create_task(copy(br, aw))
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in (t1, t2):
            t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


def make_ssl_context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="remotelink-relay", description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_RELAY_PORT)
    ap.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""),
                    help="shared access token clients must present (recommended)")
    ap.add_argument("--registry", default="relay_registry.json")
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--max-sessions", type=int, default=500)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ctx = make_ssl_context(a.cert, a.key) if a.cert and a.key else None

    async def run():
        srv = RelayServer(a.host, a.port, a.token, a.registry, ctx,
                          max_sessions=a.max_sessions)
        server = await srv.start()
        async with server:
            await server.serve_forever()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

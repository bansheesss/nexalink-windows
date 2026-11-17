"""Headless tests: python -m unittest discover -s tests -v

They use fake screen/input back-ends, so they need only `cryptography`.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import discovery  # noqa: E402
from core.filetransfer import FileTransfer, safe_name  # noqa: E402
from core.network import Connection, client_handshake  # noqa: E402
from core.protocol import (Frame, MsgType, pack_json, pack_key,  # noqa: E402
                           pack_mouse_button, pack_mouse_move, pack_scroll,
                           unpack_json, unpack_mouse_move, Codec)
from core.security import AuthError  # noqa: E402
from core.services import Services  # noqa: E402
from server.host import Host  # noqa: E402
from utils.config import Config  # noqa: E402


class FakeCap:
    def __init__(self):
        self.executor = ThreadPoolExecutor(1)
        self.index, self.n, self.closed = 1, 0, False
        self.monitor = {"left": 0, "top": 0, "width": 800, "height": 600}

    def info(self):
        return [{"index": i, "width": 800, "height": 600, "left": 0, "top": 0}
                for i in range(3)]

    def select(self, idx):
        self.index = idx

    def grab(self, codec, q, scale, force=False):
        self.n += 1
        return (800, 600, b"IMG%d-q%d" % (self.n, q)) if force or self.n % 3 == 0 else None

    def close(self):
        self.closed = True


class FakeInput:
    last = None

    def __init__(self):
        self.log, self.released = [], False
        FakeInput.last = self

    def set_monitor(self, m): self.log.append(("mon", m["width"]))
    def move(self, x, y): self.log.append(("move", round(x, 2), round(y, 2)))
    def button(self, b, p): self.log.append(("btn", b, p))
    def scroll(self, dx, dy): self.log.append(("scroll", dx, dy))
    def key(self, n, p): self.log.append(("key", n, p))
    def release_all(self): self.released = True


class FakeClip:
    def __init__(self): self.subs, self.texts = [], []
    def subscribe(self, cb): self.subs.append(cb)
    def unsubscribe(self, cb): self.subs.remove(cb)
    def set_text(self, t): self.texts.append(t)
    def emit(self, t):
        for cb in list(self.subs):
            cb(t)


class TClient:
    """Authenticated test viewer: queues messages, routes FILE_* to FileTransfer."""
    def __init__(self, conn, dest):
        self.conn = conn
        self.q = asyncio.Queue()
        self.files = FileTransfer(conn, Services(dest_dir=Path(dest)), 1)
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while True:
                t, b = await self.conn.recv()
                if t.name.startswith("FILE_"):
                    await self.files.handle(t, b)
                else:
                    await self.q.put((t, b))
        except Exception:
            pass

    async def get(self, want, timeout=5):
        async def loop():
            while True:
                t, b = await self.q.get()
                if t is want:
                    return b
        return await asyncio.wait_for(loop(), timeout)

    async def close(self):
        self.task.cancel()
        await self.conn.close()


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clip = FakeClip()
        self.notes, self.ask_answer = [], True

        async def ask(title, text):
            return self.ask_answer
        self.host_dir = Path(self.tmp.name) / "host_in"
        self.cli_dir = Path(self.tmp.name) / "cli_in"
        self.svc = Services(clipboard=self.clip, ask=ask, notify=self.notes.append,
                            dest_dir=self.host_dir)
        self.cfg = Config(machine_id="123456789", password="pw-test",
                          bind="127.0.0.1", port=0, fps=30, discoverable=False)
        self.host = Host(self.cfg, FakeCap, FakeInput, self.svc)
        srv = await self.host.start()
        self.port = srv.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.host.stop()
        self.tmp.cleanup()

    async def connect(self, pw="pw-test") -> TClient:
        c = await Connection.open("127.0.0.1", self.port)
        await client_handshake(c, pw)
        return TClient(c, self.cli_dir)

    async def test_session_input_settings_monitor(self):
        c = await self.connect()
        info = unpack_json(await c.get(MsgType.SESSION_INFO))
        self.assertEqual((info["width"], info["monitor"], len(info["monitors"])), (800, 1, 3))
        f = Frame.unpack(await c.get(MsgType.FRAME))
        self.assertEqual((f.width, f.height, f.codec), (800, 600, Codec.JPEG))
        for t, b in ((MsgType.MOUSE_MOVE, pack_mouse_move(.5, .25)),
                     (MsgType.MOUSE_BUTTON, pack_mouse_button(1, True)),
                     (MsgType.MOUSE_SCROLL, pack_scroll(0, -1)),
                     (MsgType.KEY, pack_key("enter", True))):
            await c.conn.send(t, b)
        await c.conn.send(MsgType.FRAME_REQUEST, pack_json({"quality": 45, "monitor": 2}))
        info2 = unpack_json(await c.get(MsgType.SESSION_INFO))
        self.assertEqual(info2["monitor"], 2)
        await c.conn.send(MsgType.PING, b"hi")
        self.assertEqual(await c.get(MsgType.PONG), b"hi")
        log = FakeInput.last.log
        self.assertIn(("move", 0.5, 0.25), log)
        self.assertIn(("key", "enter", True), log)
        # busy: a second viewer is refused
        c2 = await self.connect()
        self.assertIn("busy", unpack_json(await c2.get(MsgType.ERROR))["reason"])
        await c2.close()
        await c.conn.send(MsgType.BYE)
        await asyncio.sleep(0.3)
        self.assertTrue(FakeInput.last.released)
        await c.close()

    async def test_wrong_password_and_rate_limit(self):
        for _ in range(5):
            with self.assertRaises(AuthError):
                c = await Connection.open("127.0.0.1", self.port)
                try:
                    await client_handshake(c, "nope")
                finally:
                    await c.close()
        c = await Connection.open("127.0.0.1", self.port)      # now blocked
        with self.assertRaises((asyncio.IncompleteReadError, ConnectionError)):
            await client_handshake(c, "pw-test")
        await c.close()

    async def test_confirmation_denied(self):
        self.cfg.confirm_connections = True
        self.ask_answer = False
        c = await self.connect()
        self.assertIn("denied", unpack_json(await c.get(MsgType.ERROR))["reason"])
        await c.close()

    async def test_clipboard_both_ways(self):
        c = await self.connect()
        await c.get(MsgType.SESSION_INFO)
        await asyncio.sleep(0.1)
        await c.conn.send(MsgType.CLIPBOARD, "سلام hello".encode())
        await asyncio.sleep(0.2)
        self.assertEqual(self.clip.texts, ["سلام hello"])
        self.clip.emit("from host")
        self.assertEqual(await c.get(MsgType.CLIPBOARD), b"from host")
        await c.conn.send(MsgType.BYE)
        await asyncio.sleep(0.2)
        self.assertEqual(self.clip.subs, [])          # unsubscribed on disconnect
        await c.close()

    async def test_file_transfer_both_directions(self):
        c = await self.connect()
        await c.get(MsgType.SESSION_INFO)
        src = Path(self.tmp.name) / "data.bin"
        payload = os.urandom(900_000)
        src.write_bytes(payload)
        # viewer -> host
        self.assertTrue(await c.files.send_file(src))
        self.assertEqual((self.host_dir / "data.bin").read_bytes(), payload)
        # second send must not overwrite
        self.assertTrue(await c.files.send_file(src))
        self.assertTrue((self.host_dir / "data (1).bin").exists())
        # host -> viewer
        self.assertTrue(await asyncio.wait_for(self.host.send_file(src), 10) is None)
        await asyncio.sleep(0.5)
        self.assertEqual((self.cli_dir / "data.bin").read_bytes(), payload)
        # declined
        self.ask_answer = False
        self.assertFalse(await c.files.send_file(src))
        self.assertFalse(list(self.host_dir.glob("*.part")))
        await c.close()

    async def test_unsafe_filename(self):
        self.assertEqual(safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(safe_name("C:\\x\\evil?.exe"), "evil_.exe")
        self.assertEqual(safe_name("CON.txt"), "_CON.txt")
        self.assertEqual(safe_name("..."), "file")


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_find_by_id(self):
        port = 47071
        tr = await discovery.start_responder("987654321", "box", 7070, port, "127.0.0.1")
        try:
            run = asyncio.get_running_loop().run_in_executor
            hit = await run(None, lambda: discovery.discover(
                "987654321", 1.0, port, ["127.0.0.1"]))
            self.assertEqual([(h["id"], h["port"], h["host"]) for h in hit],
                             [("987654321", 7070, "127.0.0.1")])
            miss = await run(None, lambda: discovery.discover(
                "111111111", 0.6, port, ["127.0.0.1"]))
            self.assertEqual(miss, [])
        finally:
            tr.close()


class ProtocolTests(unittest.TestCase):
    def test_mouse_roundtrip(self):
        x, y = unpack_mouse_move(pack_mouse_move(.5, .25))
        self.assertAlmostEqual(x, .5, 3)
        self.assertAlmostEqual(y, .25, 3)

    def test_frame_roundtrip(self):
        f = Frame(7, 99, Codec.WEBP, 1920, 1080, b"xyz")
        self.assertEqual(Frame.unpack(f.pack()), f)


if __name__ == "__main__":
    unittest.main()

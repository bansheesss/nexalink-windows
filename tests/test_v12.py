"""v1.2: relay, one-time passwords, permissions, settings lock, address book."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_remotelink import FakeCap, FakeClip, FakeInput, TClient  # noqa: E402

from core import settingslock  # noqa: E402
from core.connect import ConnectError, open_target  # noqa: E402
from core.network import client_handshake  # noqa: E402
from core.otp import OtpStore  # noqa: E402
from core.protocol import (MsgType, pack_json, pack_key, pack_mouse_move,  # noqa: E402
                           unpack_json)
from core.relay_client import (RelayHostLink, RelaySettings, connect_via_relay,  # noqa: E402
                               identity_from_seed, new_identity_seed)
from core.relay_proto import RelayError, read_msg, write_msg  # noqa: E402
from core.security import AuthError  # noqa: E402
from core.services import Services  # noqa: E402
from relay.server import RelayServer  # noqa: E402
from server.host import Host  # noqa: E402
from utils.addressbook import AddressBook, Entry  # noqa: E402
from utils.config import Config  # noqa: E402


async def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


class RelayCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.relay = RelayServer("127.0.0.1", 0, token="tok",
                                 registry_path=str(Path(self.tmp.name) / "reg.json"))
        await self.relay.start()
        self.rs = RelaySettings("127.0.0.1", self.relay.port, "tok")
        self.states = []
        self.svc = Services(clipboard=FakeClip(), notify=lambda m: None,
                            relay_status=self.states.append,
                            dest_dir=Path(self.tmp.name) / "in")
        self.cfg = Config(machine_id="123456789", password="perm-pass", bind="127.0.0.1",
                          port=0, fps=30, discoverable=False, relay_server="127.0.0.1",
                          relay_port=self.relay.port, relay_token="tok",
                          relay_private=new_identity_seed())
        self.host = Host(self.cfg, FakeCap, FakeInput, self.svc)
        await self.host.start()
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        await self.host.stop()
        await self.relay.stop()
        self.tmp.cleanup()

    async def online(self):
        await until(lambda: self.host.relay_state == "online")

    async def viewer(self, pw, target="123456789", rs=None):
        conn = await connect_via_relay(rs or self.rs, target)
        await client_handshake(conn, pw)
        c = TClient(conn, Path(self.tmp.name) / "cli")
        self.clients.append(c)
        return c

    # ------------------------------------------------------------------ tests
    async def test_internet_session_through_relay(self):
        await self.online()
        c = await self.viewer("perm-pass")
        info = unpack_json(await c.get(MsgType.SESSION_INFO))
        self.assertEqual(info["perms"], {"control": True, "files": True, "clipboard": True})
        await c.get(MsgType.FRAME)
        await c.conn.send(MsgType.MOUSE_MOVE, pack_mouse_move(.5, .5))
        await c.conn.send(MsgType.KEY, pack_key("a", True))
        await until(lambda: ("key", "a", True) in FakeInput.last.log)
        await c.conn.send(MsgType.PING, b"12345678")
        self.assertEqual(await c.get(MsgType.PONG), b"12345678")

    async def test_wrong_password_over_relay(self):
        await self.online()
        with self.assertRaises(AuthError):
            conn = await connect_via_relay(self.rs, "123456789")
            await client_handshake(conn, "nope")

    async def test_relay_token_is_enforced(self):
        bad = RelaySettings("127.0.0.1", self.relay.port, "wrong")
        with self.assertRaises(RelayError) as cm:
            await connect_via_relay(bad, "123456789")
        self.assertEqual(cm.exception.code, "bad_token")

    async def test_offline_target(self):
        with self.assertRaises(RelayError) as cm:
            await connect_via_relay(self.rs, "999999999")
        self.assertEqual(cm.exception.code, "offline")

    async def test_open_target_falls_back_to_relay(self):
        await self.online()
        conn, mode = await open_target("123 456 789", 7070, self.rs, lan_timeout=0.3)
        self.assertEqual(mode, "relay")
        await client_handshake(conn, "perm-pass")
        await conn.close()
        with self.assertRaises(ConnectError):
            await open_target("123456789", 7070, None, lan_timeout=0.3)

    async def test_one_time_password(self):
        await self.online()
        self.cfg.permanent_enabled = False
        otp = self.host.otp.create()
        with self.assertRaises(AuthError):                     # fixed password disabled
            conn = await connect_via_relay(self.rs, "123456789")
            await client_handshake(conn, "perm-pass")
        c = await self.viewer(otp)
        await c.get(MsgType.SESSION_INFO)
        with self.assertRaises(AuthError):                     # already used
            conn = await connect_via_relay(self.rs, "123456789")
            await client_handshake(conn, otp)

    async def test_permanent_and_otp_coexist(self):
        await self.online()
        otp = self.host.otp.create()
        await (await self.viewer("perm-pass")).get(MsgType.SESSION_INFO)
        await self.clients.pop().close()
        await until(lambda: not self.host._sessions)
        await (await self.viewer(otp)).get(MsgType.SESSION_INFO)

    async def test_view_only_and_files_disabled(self):
        await self.online()
        self.cfg.allow_control = False
        self.cfg.allow_files = False
        c = await self.viewer("perm-pass")
        info = unpack_json(await c.get(MsgType.SESSION_INFO))
        self.assertFalse(info["perms"]["control"])
        await c.conn.send(MsgType.KEY, pack_key("x", True))
        await c.conn.send(MsgType.PING, b"abcdefgh")
        await c.get(MsgType.PONG)
        self.assertNotIn(("key", "x", True), FakeInput.last.log)
        src = Path(self.tmp.name) / "f.txt"
        src.write_text("hi")
        self.assertFalse(await c.files.send_file(src))
        self.assertFalse((Path(self.tmp.name) / "in").exists())

    async def test_id_taken_assigns_new_id(self):
        await self.online()
        ids, states = [], []
        link = RelayHostLink(
            self.rs, "123456789", identity_from_seed(new_identity_seed()),
            lambda r, w, p: asyncio.sleep(0), states.append,
            on_id_taken=lambda: ids.append("222222222") or "222222222")
        link.start()
        try:
            await until(lambda: link.online)
            self.assertEqual(ids, ["222222222"])
            self.assertEqual(link.id, "222222222")
        finally:
            await link.stop()

    async def test_registry_survives_restart_and_blocks_hijack(self):
        await self.online()
        await self.relay.stop()
        relay2 = RelayServer("127.0.0.1", 0, token="tok",
                             registry_path=str(Path(self.tmp.name) / "reg.json"))
        await relay2.start()
        try:
            rs2 = RelaySettings("127.0.0.1", relay2.port, "tok")
            link = RelayHostLink(rs2, "123456789", identity_from_seed(new_identity_seed()),
                                 lambda r, w, p: asyncio.sleep(0), lambda s: None)
            link.start()
            await asyncio.sleep(0.6)
            self.assertFalse(link.online)          # different key: ID is not hijackable
            await link.stop()
        finally:
            await relay2.stop()

    async def test_connect_rate_limit(self):
        r = RelayServer("127.0.0.1", 0, connects_per_min=3)
        await r.start()
        try:
            rs = RelaySettings("127.0.0.1", r.port)
            codes = []
            for _ in range(4):
                try:
                    await connect_via_relay(rs, "999999999")
                except RelayError as e:
                    codes.append(e.code)
            self.assertEqual(codes, ["offline"] * 3 + ["rate_limited"])
        finally:
            await r.stop()

    async def test_garbage_does_not_crash_relay(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.relay.port)
        w.write(b"\xff\xff\xff\xffgarbage")
        await w.drain()
        w.close()
        await asyncio.sleep(0.1)
        r, w = await asyncio.open_connection("127.0.0.1", self.relay.port)
        await write_msg(w, {"t": "connect", "v": 1, "id": "bad", "token": "tok"})
        self.assertEqual((await read_msg(r))["code"], "bad_request")
        w.close()


class UnitTests(unittest.TestCase):
    def test_otp_expiry_limit_and_revocation(self):
        now = [0.0]
        s = OtpStore(ttl_seconds=60, length=8, max_failures=3, clock=lambda: now[0])
        a = s.create()
        self.assertEqual(len(a), 8)
        self.assertTrue(s.consume(a))
        self.assertFalse(s.consume(a))
        b = s.create()
        now[0] = 61
        self.assertFalse(s.consume(b))                     # expired
        c = s.create()
        for _ in range(3):
            s.note_failure()
        self.assertEqual(s.active(), [])                   # revoked after guessing
        self.assertFalse(s.consume(c))
        for _ in range(5):
            s.create()
        self.assertLessEqual(len(s.active()), OtpStore.MAX_ACTIVE)

    def test_settings_lock(self):
        h = settingslock.make_hash("s3cret")
        self.assertTrue(settingslock.verify(h, "s3cret"))
        self.assertFalse(settingslock.verify(h, "wrong"))
        self.assertFalse(settingslock.verify("garbage", "x"))
        t = [0.0]
        th = settingslock.Throttle(clock=lambda: t[0])
        th.failed(); th.failed(); th.failed()
        self.assertEqual(th.wait_seconds(), 4.0)
        t[0] = 5
        self.assertEqual(th.wait_seconds(), 0.0)

    def test_address_book_labels_and_search(self):
        with tempfile.TemporaryDirectory() as d:
            ab = AddressBook(Path(d) / "ab.json")
            ab.upsert(Entry("123456789", "لپ‌تاپ دفتر", ["کار"], "طبقه دوم"))
            ab.upsert(Entry("192.168.1.5", "", ["خانه"]))
            ab.touch("192.168.1.5")
            ab2 = AddressBook(Path(d) / "ab.json")         # persisted
            self.assertEqual(ab2.entries["123456789"].label, "لپ‌تاپ دفتر")
            self.assertEqual([e.key for e in ab2.search("دفتر")], ["123456789"])
            self.assertEqual(ab2.tags(), ["خانه", "کار"])
            self.assertEqual(ab2.sorted()[0].key, "192.168.1.5")   # most recent first
            ab2.entries["123456789"].label = "ویرایش‌شده"
            ab2.save()
            self.assertEqual(AddressBook(Path(d) / "ab.json").entries["123456789"].label, "ویرایش‌شده")
            ab2.remove("123456789")
            self.assertNotIn("123456789", AddressBook(Path(d) / "ab.json").entries)

    def test_portable_mode_keeps_everything_local(self):
        from utils import paths
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("REMOTELINK_HOME")
            os.environ["REMOTELINK_HOME"] = d
            try:
                self.assertTrue(paths.is_portable())
                self.assertEqual(paths.data_dir(), Path(d))
                self.assertEqual(paths.default_download_dir(), Path(d) / "received")
                from utils.config import Config as C
                cfg = C.load()
                self.assertTrue((Path(d) / "config.json").exists())
                self.assertEqual(len(cfg.relay_private), 64)       # identity generated
                self.assertEqual(len(cfg.machine_id), 9)
            finally:
                if old is None:
                    del os.environ["REMOTELINK_HOME"]
                else:
                    os.environ["REMOTELINK_HOME"] = old


if __name__ == "__main__":
    unittest.main()

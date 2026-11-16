"""Smoke test for the Qt UI logic WITHOUT PyQt6: every Qt class is replaced by a
permissive dummy, so this only catches NameError/AttributeError/typos in our own
code paths (it does not test rendering). Run: python tests/ui_smoke.py"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _make(name)

    def __or__(cls, other):
        return cls


class _Base:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _make(name)()

    def __call__(self, *a, **k):
        return _make("ret")()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True

    __lt__ = __gt__ = __le__ = __ge__ = lambda self, o: False
    __int__ = lambda self: 0
    __index__ = lambda self: 0


def _make(name):
    return _Meta(name, (_Base,), {})


def stub(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda attr: _make(attr)
    sys.modules[name] = m
    return m


for n in ("PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets"):
    stub(n)

os.environ["REMOTELINK_HOME"] = tempfile.mkdtemp()
from core import settingslock                       # noqa: E402
from core.security import generate_password         # noqa: E402
from ui import main_window as mw                    # noqa: E402
from ui import settings_dialog as sd                # noqa: E402
from utils.addressbook import AddressBook, Entry    # noqa: E402
from utils.config import Config                     # noqa: E402

cfg = Config.load()
win = mw.MainWindow(cfg)
hp, vp = win.host_panel, win.viewer_panel

# host panel: credentials, relay states, OTP flow
hp.refresh_credentials()
for st in ("off", "connecting", "online", "offline", "error:bad_token", "error:id_taken", "error:x"):
    hp._set_relay(st)
hp._running(True)
hp._running(False)


class FakeOtp:
    ttl, length = 600, 8
    def create(self): return "Abcd1234"
    def remaining(self, pw): return 125.0
fake = types.SimpleNamespace(otp=FakeOtp(), start_relay=lambda: None)
hp.host = fake
hp._new_otp()
hp._tick_otp()
hp.apply_settings()
hp.host = None
hp.shutdown()

# viewer panel: address book
vp.host_edit = types.SimpleNamespace(text=lambda: "123 456 789", setText=lambda t: None)
vp.book.upsert(Entry("123456789", "لپ‌تاپ", ["کار"]))
vp._reload_book()
vp._book_picked(1)

# settings gate / lock
assert settingslock.verify(settingslock.make_hash("x"), "x")
sd._throttle.failed()
print("UI smoke OK")

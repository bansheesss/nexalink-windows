"""Where RemoteLink keeps its data. Portable mode keeps everything next to the
program so it can run from a USB stick without installing or admin rights."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PORTABLE_FLAG = "portable.flag"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def is_portable() -> bool:
    if os.environ.get("REMOTELINK_HOME"):
        return True
    return (app_dir() / PORTABLE_FLAG).exists() and _writable(app_dir() / "data")


def data_dir() -> Path:
    env = os.environ.get("REMOTELINK_HOME")
    if env:
        return Path(env)
    if (app_dir() / PORTABLE_FLAG).exists() and _writable(app_dir() / "data"):
        return app_dir() / "data"
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "remotelink"


def default_download_dir() -> Path:
    if is_portable():
        return data_dir() / "received"
    return Path.home() / "Downloads" / "RemoteLink"

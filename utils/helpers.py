from __future__ import annotations

import logging
import os
import socket
import sys


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def local_ips() -> list[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # connect() on UDP sends nothing; it just picks the outgoing interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def enable_dpi_awareness() -> None:
    """Windows: make screen/mouse coordinates real pixels (no 125%/150% drift).
    Only for the console host; Qt does this itself for GUI apps."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def session_warning() -> str | None:
    if sys.platform.startswith("linux") and \
            os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return ("Wayland session detected: screen capture and input injection "
                "usually don't work. Log in with an X11 session instead.")
    return None

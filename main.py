"""ViunaLink – single entry point.

  python main.py              → پنجره یکپارچه (میزبان + بیننده)
  python main.py host         → فقط میزبان (قدیمی)
  python main.py host --cli   → میزبان بدون پنجره
  python main.py client       → فقط بیننده (قدیمی)

برای ساخت EXE:
  pip install pyinstaller
  python build.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from utils.helpers import (enable_dpi_awareness, local_ips, session_warning,
                           setup_logging)

__version__ = "1.2.0"


def _host_config(args):
    from utils.config import Config
    cfg = Config.load()
    if args.new_password:
        from core.security import generate_password
        cfg.password = generate_password()
        cfg.save()
    if args.password:
        cfg.password = args.password
    for key in ("port", "fps", "scale", "codec", "monitor"):
        val = getattr(args, key)
        if val is not None:
            setattr(cfg, key, val)
    if args.quality is not None:
        cfg.jpeg_quality = args.quality
    if args.confirm:
        cfg.confirm_connections = True
    return cfg


def run_host(args) -> int:
    cfg = _host_config(args)
    if not args.cli:
        from server.host_gui import run_host_gui
        return run_host_gui(cfg)

    from server.host import Host
    from utils.config import config_path
    enable_dpi_awareness()
    print("=" * 50)
    print(f"  ViunaLink host   ID: {cfg.machine_id}")
    print(f"  Password: {cfg.password}")
    print(f"  Port: {cfg.port}   Addresses: {', '.join(local_ips()) or 'n/a'}")
    print(f"  Config: {config_path()}")
    print("=" * 50)
    warn = session_warning()
    if warn:
        print("WARNING:", warn)
    host = Host(cfg)
    if args.otp:
        print(f"  One-time password (valid {cfg.otp_ttl_minutes} min, single use): {host.otp.create()}")
    try:
        asyncio.run(host.run())
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as e:
        print(f"Cannot start: {e}")
        return 1
    return 0


def run_relay(args) -> int:
    from relay.server import main as relay_main
    argv = ["--port", str(args.port), "--registry", args.registry]
    if args.token:
        argv += ["--token", args.token]
    if args.cert and args.key:
        argv += ["--cert", args.cert, "--key", args.key]
    return relay_main(argv)


def run_client(args) -> int:
    from client.gui import run_client_gui
    return run_client_gui(args.host, args.port, args.password)


def run_unified(_args) -> int:
    """Single-window app (recommended)."""
    enable_dpi_awareness()
    from ui.main_window import run_app
    return run_app()


def main() -> int:
    p = argparse.ArgumentParser(prog="viunalink")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"ViunaLink {__version__}")
    p.add_argument("--reset-settings-lock", action="store_true",
                   help="remove the settings password (needs access to this computer)")
    sub = p.add_subparsers(dest="mode")

    h = sub.add_parser("host", help="share this machine")
    h.add_argument("--cli", action="store_true", help="headless, no window")
    h.add_argument("--port", type=int)
    h.add_argument("--password")
    h.add_argument("--new-password", action="store_true")
    h.add_argument("--quality", type=int, help="JPEG/WebP quality 10-95")
    h.add_argument("--fps", type=int)
    h.add_argument("--scale", type=float, help="0.25-1.0 frame downscale")
    h.add_argument("--codec", choices=["jpeg", "webp"])
    h.add_argument("--monitor", type=int, help="0=all, 1=primary, 2=second...")
    h.add_argument("--otp", action="store_true",
                   help="print a one-time password (headless host)")
    h.add_argument("--confirm", action="store_true",
                   help="ask before accepting a viewer (GUI host)")
    h.set_defaults(func=run_host)

    r = sub.add_parser("relay", help="run a relay server (internet access)")
    r.add_argument("--port", type=int, default=7072)
    r.add_argument("--token", default="")
    r.add_argument("--registry", default="relay_registry.json")
    r.add_argument("--cert")
    r.add_argument("--key")
    r.set_defaults(func=run_relay)

    c = sub.add_parser("client", help="connect to a host")
    c.add_argument("--host", help="IP address or 9-digit ID")
    c.add_argument("--port", type=int, default=7070)
    c.add_argument("--password")
    c.set_defaults(func=run_client)

    args = p.parse_args()
    setup_logging(args.verbose)
    if args.reset_settings_lock:
        from utils.config import Config
        cfg = Config.load()
        cfg.settings_lock = ""
        cfg.save()
        print("Settings password removed; you will be asked to create a new one.")
        return 0
    if args.mode is None:
        return run_unified(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

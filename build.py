"""Build with PyInstaller.

    pip install pyinstaller
    python build.py --portable     # Windows/Linux folder + zip, runs without install/admin
    python build.py --onefile      # single executable (extracts to a temp dir on start)

--portable creates dist/NexaLink/ with a `portable.flag` file next to the
program, so ALL data (config, address book, received files) lives in
dist/NexaLink/data/ and nothing is written to the registry or user profile.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORTABLE_README = """NexaLink - portable
=====================
Run NexaLink.exe. No installation and no administrator rights are needed.
Everything (settings, address book, received files) is stored in the "data" folder
next to the program: copy/move the whole folder (e.g. on a USB stick) and it keeps working.

Internet access: set your relay server in Settings > Network. This only needs
OUTBOUND connections, so it works without any firewall change.
Local network (direct): Windows may ask to allow incoming connections; allowing it
needs admin rights. If you decline, use the relay or connect from this PC outwards.

Limits without admin: elevated (Run as administrator) windows, UAC prompts and the
lock screen cannot be controlled. Windows SmartScreen may warn because the file is
not code-signed: choose "More info" > "Run anyway".
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portable", action="store_true", help="folder build + portable.flag + zip")
    ap.add_argument("--onefile", action="store_true", help="single-file executable")
    a = ap.parse_args()

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", "NexaLink", "--paths", str(ROOT)]
    cmd.append("--onefile" if a.onefile else "--onedir")
    backend = "_win32" if sys.platform == "win32" else "_xorg"
    for mod in ("keyboard", "mouse"):
        cmd += ["--hidden-import", f"pynput.{mod}.{backend}"]
    for pkg in ("client", "server", "core", "utils", "ui", "relay"):
        cmd += ["--hidden-import", pkg]
    icon = ROOT / "icon.ico"
    if icon.exists():
        cmd += ["--icon", str(icon)]
    cmd.append(str(ROOT / "main.py"))
    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc or not a.portable or a.onefile:
        return rc

    out = ROOT / "dist" / "NexaLink"
    (out / "portable.flag").write_text("portable mode: data is kept in ./data\n")
    (out / "README-PORTABLE.txt").write_text(PORTABLE_README, "utf-8")
    plat = "win64" if sys.platform == "win32" else sys.platform
    archive = shutil.make_archive(str(ROOT / "dist" / f"NexaLink-portable-{plat}"),
                                  "zip", ROOT / "dist", "NexaLink")
    print("Portable package:", archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())

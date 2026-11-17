"""Build with PyInstaller.

    pip install pyinstaller
    python build.py --portable     # Windows/Linux folder + zip, runs without install/admin
    python build.py --onefile      # single executable (extracts to a temp dir on start)

--portable creates dist/ViunaLink/ with a `portable.flag` file next to the
program, so ALL data (config, address book, received files) lives in
dist/ViunaLink/data/ and nothing is written to the registry or user profile.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORTABLE_README = """ViunaLink - portable
=====================
Run ViunaLink.exe. No installation and no administrator rights are needed.
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
           "--name", "ViunaLink", "--paths", str(ROOT)]
    cmd.append("--onefile" if a.onefile else "--onedir")
    backend = "_win32" if sys.platform == "win32" else "_xorg"
    for mod in ("keyboard", "mouse"):
        cmd += ["--hidden-import", f"pynput.{mod}.{backend}"]
    for pkg in ("client", "server", "core", "utils", "ui", "relay"):
        cmd += ["--hidden-import", pkg]
    # Draw the ViunaLink mark into a multi-resolution Windows icon at build time.
    from PIL import Image, ImageDraw
    icon = ROOT / "dist" / "viunalink.ico"
    icon.parent.mkdir(parents=True, exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256)
    frames = []
    for size in sizes:
        im = Image.new("RGBA", (size, size), (7, 23, 46, 255))
        d = ImageDraw.Draw(im)
        scale = size / 108
        def pt(x, y): return (round(x * scale), round(y * scale))
        width = max(2, round(6 * scale))
        d.line([pt(36, 54), pt(72, 28)], fill=(85, 214, 194, 255), width=width)
        d.line([pt(36, 54), pt(72, 80)], fill=(85, 214, 194, 255), width=width)
        radius = max(3, round(9 * scale))
        for x, y in ((36, 54), (72, 28), (72, 80)):
            cx, cy = pt(x, y)
            d.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=(244, 247, 255, 255))
        frames.append(im)
    frames[-1].save(icon, format="ICO", sizes=[(s, s) for s in sizes])
    cmd += ["--icon", str(icon)]
    cmd.append(str(ROOT / "main.py"))
    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc or not a.portable or a.onefile:
        return rc

    out = ROOT / "dist" / "ViunaLink"
    (out / "portable.flag").write_text("portable mode: data is kept in ./data\n")
    (out / "README-PORTABLE.txt").write_text(PORTABLE_README, "utf-8")
    plat = "win64" if sys.platform == "win32" else sys.platform
    archive = shutil.make_archive(str(ROOT / "dist" / f"ViunaLink-portable-{plat}"),
                                  "zip", ROOT / "dist", "ViunaLink")
    print("Portable package:", archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())

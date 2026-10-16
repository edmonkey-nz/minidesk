#!/usr/bin/env python3
"""Package the minidesk uploader as a single app for this OS with PyInstaller.

PyInstaller can't cross-compile: run this on each OS (CI does, see
.github/workflows/release.yml).

    pip install -r tools/requirements-uploader.txt pyinstaller
    pio run -e c3                       # builds the firmware to bundle
    python3 tools/build_uploader.py     # -> dist/
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
NAME = "minidesk-uploader"


def git_version() -> str:
    """The release tag (e.g. "v0.1.0") when building exactly a tag with no local
    edits, otherwise "development build"."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)

    try:
        tag = git("describe", "--tags", "--exact-match")
        clean = git("diff", "--quiet", "HEAD").returncode == 0  # tracked files only
    except OSError:
        return "development build"
    return tag.stdout.strip() if tag.returncode == 0 and clean else "development build"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--firmware", type=Path, default=ROOT / ".pio/build/c3/firmware.factory.bin")
    args = ap.parse_args()

    if not args.firmware.is_file():
        print(f"error: no firmware at {args.firmware}. Run `pio run -e c3` first.", file=sys.stderr)
        return 1

    import PyInstaller.__main__

    BUILD.mkdir(exist_ok=True)
    info = BUILD / "build_info.json"
    info.write_text(json.dumps({"version": git_version(), "date": datetime.date.today().isoformat()}))

    system = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")
    sep = os.pathsep
    opts = [
        str(TOOLS / "minidesk_uploader.py"),
        "--name", NAME,
        "--windowed",
        "--noconfirm",
        "--clean",
        "--paths", str(TOOLS),
        "--add-data", f"{args.firmware}{sep}firmware",
        "--add-data", f"{info}{sep}.",
        "--collect-all", "esptool",  # stub flasher images are data files
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
    ]
    # macOS wants an .app bundle (onedir); elsewhere one self-contained file.
    opts.append("--onedir" if system == "macos" else "--onefile")
    PyInstaller.__main__.run(opts)

    # Name the result per OS so a release can carry all three.
    if system == "macos":
        # ditto keeps the .app's symlinks and signature; shutil's zip doesn't.
        out = DIST / f"{NAME}-macos.zip"
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(DIST / f"{NAME}.app"), str(out)], check=True)
    elif system == "windows":
        out = DIST / f"{NAME}-windows.exe"
        (DIST / f"{NAME}.exe").replace(out)
    else:
        out = DIST / f"{NAME}-linux"
        (DIST / NAME).replace(out)
    print(f"built {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

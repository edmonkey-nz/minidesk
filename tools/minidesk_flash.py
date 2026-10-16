#!/usr/bin/env python3
"""Convert a QLC+ workspace and upload its scenes to a minidesk over USB.

This is the engine behind the minidesk uploader app, and a CLI in its own right:

    python3 tools/minidesk_flash.py qlc/show.qxw                 # upload scenes
    python3 tools/minidesk_flash.py qlc/show.qxw --firmware fw.bin  # new box: firmware too
    python3 tools/minidesk_flash.py --list-ports

Scenes go into the box's LittleFS partition as /scenes.json, the same file
`pio run -t uploadfs` writes, so firmware and pio workflow are unchanged.
The partition is found by reading the box's own partition table, not hard-coded.

Nothing is written if the workspace has errors. DMX stops while the box is in its
bootloader, so fixtures must be safe without DMX: off, unplugged, or tested to go
dark (see CLAUDE.md, "Fixture notes").
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import qxw_to_scenes as conv

ESPRESSIF_VID = 0x303A
CHIP = "esp32c3"

PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0xC00
PARTITION_MAGIC = b"\xaa\x50"
FS_PARTITION_LABEL = "spiffs"  # LittleFS.begin() default label in Arduino-ESP32
DATA_TYPE = 0x01
FS_SUBTYPES = (0x82, 0x83)  # spiffs, littlefs

SCENES_FILE = "scenes.json"
SCENES_MAX_BYTES = 48 * 1024  # must match SCENES_MAX_BYTES in include/config.h

# Must match the firmware's LittleFS build (CONFIG_LITTLEFS_* in the Arduino-ESP32
# sdkconfig) or the box won't mount the image and boots with "FS Err".
LFS_CONFIG = dict(
    block_size=4096,
    read_size=128,
    prog_size=128,
    cache_size=512,
    lookahead_size=128,
    block_cycles=512,
    name_max=64,
    disk_version=0x00020001,
)

Log = Callable[[str], None]


class FlashError(Exception):
    pass


@dataclass
class Partition:
    label: str
    type: int
    subtype: int
    offset: int
    size: int


@dataclass
class Port:
    device: str
    description: str
    is_esp: bool


# --- conversion ---------------------------------------------------------------


@dataclass
class Converted:
    scenes: list[dict]
    warnings: list[str]
    errors: list[str]
    json_text: str | None  # None when there are errors


def convert(qxw: Path) -> Converted:
    """Run the converter. Any error means json_text is None and nothing may be written."""
    res = conv.convert_workspace(qxw)
    errors = list(res.errors)
    text = None
    if not errors:
        text = conv.render_json(qxw.name, res.scenes, res.fixtures)
        errors += [f"internal: {p}" for p in conv.validate_document(json.loads(text))]
        if len(text.encode()) > SCENES_MAX_BYTES:
            errors.append(f"scenes.json is {len(text.encode())} bytes; the box accepts at most {SCENES_MAX_BYTES}")
        if errors:
            text = None
    return Converted(res.scenes, list(res.warnings), errors, text)


# --- partition table and image -----------------------------------------------------


def parse_partition_table(data: bytes) -> list[Partition]:
    parts = []
    for i in range(0, len(data) - 31, 32):
        entry = data[i : i + 32]
        if entry[:2] != PARTITION_MAGIC:
            break  # 0xEBEB MD5 entry or erased flash ends the table
        ptype, subtype, offset, size = struct.unpack_from("<BBII", entry, 2)
        label = entry[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
        parts.append(Partition(label, ptype, subtype, offset, size))
    return parts


def find_fs_partition(parts: list[Partition]) -> Partition:
    for p in parts:
        if p.label == FS_PARTITION_LABEL and p.type == DATA_TYPE and p.subtype in FS_SUBTYPES:
            return p
    found = ", ".join(p.label for p in parts) or "none"
    raise FlashError(f"no '{FS_PARTITION_LABEL}' data partition on the box (found: {found}). Install firmware first.")


def build_fs_image(scenes_json: str, size: int) -> bytes:
    from littlefs import LittleFS

    block = LFS_CONFIG["block_size"]
    if size % block:
        raise FlashError(f"partition size {size:#x} is not a multiple of {block}")
    fs = LittleFS(block_count=size // block, **LFS_CONFIG)
    with fs.open(SCENES_FILE, "w") as f:
        f.write(scenes_json)
    return bytes(fs.context.buffer)


# --- ports ---------------------------------------------------------------------------


def list_ports() -> list[Port]:
    from serial.tools import list_ports as lp

    ports = []
    for p in lp.comports():
        is_esp = p.vid == ESPRESSIF_VID
        desc = p.description if p.description and p.description != "n/a" else ""
        if not is_esp and not desc:
            continue  # hide the dozens of legacy /dev/ttyS* on Linux
        ports.append(Port(p.device, desc or p.device, is_esp))
    ports.sort(key=lambda p: (not p.is_esp, p.device))
    return ports


# --- flashing -----------------------------------------------------------------------


def _install_logger(log: Log) -> None:
    from esptool.logger import TemplateLogger, log as esplog

    class _Logger(TemplateLogger):
        _last_pct = -1

        def print(self, *args, **kwargs):
            msg = " ".join(str(a) for a in args).rstrip()
            if msg:
                log(msg)

        def note(self, message):
            log(f"Note: {message}")

        def warning(self, message):
            log(f"Warning: {message}")

        def error(self, message):
            log(f"Error: {message}")

        def stage(self, finish=False):
            pass

        def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
            pct = int(100 * cur_iter / total_iters) if total_iters else 100
            if pct // 10 != self._last_pct // 10 or pct == 100:
                self._last_pct = pct
                log(f"{prefix.strip()} {pct}%")

        def set_verbosity(self, verbosity):
            pass

    esplog.set_logger(_Logger())


def _write(port: str, log: Log, plan: Callable[[object], list[tuple[int, bytes]]]) -> None:
    """Connect, let `plan(esp)` decide what to write, write it, verify, reset."""
    from esptool.cmds import attach_flash, detect_chip, reset_chip, run_stub, write_flash

    _install_logger(log)
    try:
        with detect_chip(port) as esp:
            if esp.CHIP_NAME.lower().replace("-", "") != CHIP:
                raise FlashError(f"this is an {esp.CHIP_NAME}, not an ESP32-C3")
            esp = run_stub(esp)
            attach_flash(esp)
            writes = plan(esp)
            write_flash(esp, writes)  # verifies each region by MD5
            reset_chip(esp, "hard-reset")
    except FlashError:
        raise
    except Exception as e:  # esptool raises FatalError, serial errors, OSError...
        raise FlashError(f"{type(e).__name__}: {e}") from e


def upload_scenes(port: str, scenes_json: str, log: Log) -> None:
    def plan(esp):
        from esptool.cmds import read_flash

        table = read_flash(esp, PARTITION_TABLE_OFFSET, PARTITION_TABLE_SIZE, no_progress=True)
        part = find_fs_partition(parse_partition_table(table))
        log(f"Scenes partition '{part.label}' at {part.offset:#x}, {part.size // 1024} KB")
        return [(part.offset, build_fs_image(scenes_json, part.size))]

    _write(port, log, plan)


def install_firmware(port: str, factory_bin: Path, scenes_json: str, log: Log) -> None:
    """Write the merged firmware image at 0x0, plus a scenes partition to match its table."""
    fw = factory_bin.read_bytes()
    table = fw[PARTITION_TABLE_OFFSET : PARTITION_TABLE_OFFSET + PARTITION_TABLE_SIZE]
    part = find_fs_partition(parse_partition_table(table))  # fails fast on a non-factory .bin
    image = build_fs_image(scenes_json, part.size)
    log(f"Firmware {len(fw) // 1024} KB at 0x0, scenes at {part.offset:#x}")
    _write(port, log, lambda esp: [(0x0, fw), (part.offset, image)])


def read_boot_report(port: str, timeout_s: float = 6.0) -> list[str]:
    """After a reset, catch the box's boot log lines up to its [load] summary.

    Best effort: USB re-enumerates on reset, so this may miss the log. Returns
    whatever lines were seen (possibly none).
    """
    import serial

    deadline = time.monotonic() + timeout_s
    ser = None
    while ser is None and time.monotonic() < deadline:
        try:
            ser = serial.Serial(port, 115200, timeout=0.2)
        except (serial.SerialException, OSError):
            time.sleep(0.05)
    if ser is None:
        return []
    lines: list[str] = []
    buf = b""
    with ser:
        while time.monotonic() < deadline:
            buf += ser.read(256)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()
                if line:
                    lines.append(line)
                if line.startswith("[load]") and "loaded" in line:
                    return lines
    return lines


def load_summary(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith("[load]") and "loaded" in line:
            return line[len("[load]") :].strip()
    return None


# --- CLI -----------------------------------------------------------------------------


def _pick_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    esps = [p for p in list_ports() if p.is_esp]
    if len(esps) == 1:
        return esps[0].device
    if not esps:
        raise FlashError("no minidesk found on USB (looked for an Espressif USB device). Use --port.")
    raise FlashError(f"several Espressif devices: {', '.join(p.device for p in esps)}. Use --port.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace", nargs="?", type=Path, help="QLC+ workspace (.qxw)")
    ap.add_argument("--port", help="serial port (default: the one Espressif device found)")
    ap.add_argument("--firmware", type=Path, help="also install this merged firmware image (firmware.factory.bin)")
    ap.add_argument("--yes", action="store_true", help="don't ask to confirm fixtures are safe without DMX")
    ap.add_argument("--list-ports", action="store_true")
    args = ap.parse_args(argv)

    if args.list_ports:
        for p in list_ports():
            print(f"{'*' if p.is_esp else ' '} {p.device:<16} {p.description}")
        return 0
    if not args.workspace:
        ap.error("give a .qxw workspace")

    c = convert(args.workspace)
    for w in c.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in c.errors:
        print(f"error: {e}", file=sys.stderr)
    if c.json_text is None:
        print("nothing uploaded", file=sys.stderr)
        return 1
    conv.print_summary(c.scenes)

    try:
        port = _pick_port(args.port)
        if not args.yes:
            ans = input(
                f"Upload to {port}. DMX stops while flashing. "
                "Are the fixtures safe without DMX (off, unplugged, or tested)? [y/N] "
            )
            if ans.strip().lower() not in ("y", "yes"):
                print("cancelled")
                return 1
        if args.firmware:
            install_firmware(port, args.firmware, c.json_text, print)
        else:
            upload_scenes(port, c.json_text, print)
    except FlashError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    summary = load_summary(read_boot_report(port))
    print(f"box reports: {summary}" if summary else "uploaded; check the box's display for the scene count")
    return 0


if __name__ == "__main__":
    sys.exit(main())

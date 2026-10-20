"""Tests for tools/minidesk_flash.py that don't need a box plugged in.

Needs the uploader deps: pip install -r tools/requirements-uploader.txt
"""
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
littlefs = pytest.importorskip("littlefs")
import minidesk_flash as mf  # noqa: E402

SAMPLE = Path(__file__).with_name("sample.qxw")


def entry(label: str, ptype: int, subtype: int, offset: int, size: int) -> bytes:
    return mf.PARTITION_MAGIC + struct.pack("<BBII", ptype, subtype, offset, size) + label.encode().ljust(16, b"\0") + b"\0" * 4


# Arduino-ESP32 default.csv for 4 MB, as read off a real box
DEFAULT_TABLE = (
    entry("nvs", 1, 0x02, 0x9000, 0x5000)
    + entry("otadata", 1, 0x00, 0xE000, 0x2000)
    + entry("app0", 0, 0x10, 0x10000, 0x140000)
    + entry("app1", 0, 0x11, 0x150000, 0x140000)
    + entry("spiffs", 1, 0x82, 0x290000, 0x160000)
    + entry("coredump", 1, 0x03, 0x3F0000, 0x10000)
    + b"\xeb\xeb" + b"\xff" * 30  # MD5 entry ends the table
    + b"\xff" * 64
)


def test_partition_table_parsed():
    parts = mf.parse_partition_table(DEFAULT_TABLE)
    assert [p.label for p in parts] == ["nvs", "otadata", "app0", "app1", "spiffs", "coredump"]
    fs = mf.find_fs_partition(parts)
    assert (fs.offset, fs.size) == (0x290000, 0x160000)


def test_missing_fs_partition_is_an_error():
    with pytest.raises(mf.FlashError, match="Install firmware"):
        mf.find_fs_partition(mf.parse_partition_table(b"\xff" * 0xC00))


def test_image_round_trips_with_firmware_settings():
    text = json.dumps({"version": 1, "scenes": []})
    img = mf.build_fs_image(text, 0x160000)
    assert len(img) == 0x160000
    fs = littlefs.LittleFS(block_count=len(img) // 4096, mount=False, **mf.LFS_CONFIG)
    fs.context.buffer[:] = img
    fs.mount()
    st = fs.fs_stat()
    # What the firmware's LittleFS expects (same as pio's mklittlefs image)
    assert (st.block_size, st.name_max, st.disk_version) == (4096, 64, 0x00020001)
    assert fs.listdir("/") == ["scenes.json"]
    with fs.open("scenes.json", "r") as f:
        assert f.read() == text


def test_convert_sample():
    c = mf.convert(SAMPLE)
    assert c.errors == [] and c.json_text
    assert mf.conv.validate_document(json.loads(c.json_text)) == []
    assert [s["button"] for s in c.scenes] == [1, 2]


def test_convert_errors_mean_no_json(tmp_path):
    ws = tmp_path / "bad.qxw"
    ws.write_text(SAMPLE.read_text().replace('Name="1 Tunnel"', 'Name="8 Tunnel"'))
    c = mf.convert(ws)
    assert c.errors and c.json_text is None


def test_load_summary():
    lines = ["[scene] BLANK (boot)", "[dmx] ok, 512 slots", "[load] 2 SCEnES (loaded 2, rejected 0)"]
    assert mf.load_summary(lines) == "2 SCEnES (loaded 2, rejected 0)"
    assert mf.load_summary([]) is None


def test_logger_survives_a_second_flash():
    """esptool 5.6 can't re-install a logger; the app's second upload used to die
    with "'_LegacyLoggerAdapter' object has no attribute 'set_logger'"."""
    pytest.importorskip("esptool")
    from esptool.logger import log as esplog

    first, second = [], []
    mf._install_logger(first.append)
    mf._install_logger(second.append)
    esplog.print("hello")
    assert first == [] and second == ["hello"]

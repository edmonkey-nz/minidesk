# minidesk

A pocket DMX controller that recalls up to seven static scenes on a DMX laser, plus a hard-wired BLANK button.
It's an ESP32-C3 with a TM1638 "LED&KEY" panel and a TTL-to-RS485 module. Scenes are authored in QLC+ and converted to a JSON file on the C3's flash.

```
 [ 1 Tunnel ]   <- 8-digit display: scene number + label; last decimal point = DMX heartbeat
 o o o o o o o o <- LED above the active button
 1 2 3 4 5 6 7 8 <- 1–7 scenes, 8 = BLANK
```

Read `CLAUDE.md` for the hard constraints (laser safety, never block, pin rules).

## Parts

- ESP32-C3 SuperMini (the 0.42" OLED variant works; the OLED is unused for now)
- TM1638 LED&KEY module (8 buttons, 8 LEDs, 8× 7-seg)
- TTL to RS485 module (auto-direction type, e.g. ePartners TP2003), or a MAX3485 breakout
- 2 × 10k resistors
- 5-pin XLR female (or 3-pin plus an adaptor for the laser)

## Wiring

| From | To | Notes |
|---|---|---|
| C3 3V3 | TM1638 VCC, RS485 VCC | **3.3 V only for the TM1638** |
| C3 GND | TM1638 GND, RS485 GND, XLR pin 1 | |
| GPIO 3 | TM1638 STB | |
| GPIO 4 | TM1638 CLK | |
| GPIO 10 | TM1638 DIO | 10k pull-up to 3V3 |
| GPIO 7 | RS485 TXD | 10k pull-up to 3V3 |
| — | RS485 RXD | leave unconnected |
| RS485 A+ | XLR pin 3 (Data+) | |
| RS485 B− | XLR pin 2 (Data−) | swap A/B first if nothing responds |
| — | RS485 Earth terminal | leave unconnected |

Power the C3 from USB-C. The laser has its own mains supply.

## Making scenes

1. **Install the fixture.** Copy `qlc/Dual-head-Laser2.qxf` into your QLC+ user fixtures folder (`~/.qlcplus/fixtures/` on Linux). Restart QLC+.
2. **Patch the laser** in QLC+, with your USB-DMX adaptor as the output on universe 1, at the address set on the laser.
3. **Build scenes while watching the laser.** Name each with its button number first: `1 Tunnel`, `2 Fan sweep` and so on. Only buttons 1–7 can be used. Anything without a leading number is ignored, so scratch scenes are fine.
   - Set CH1 (Run mode) to 64–127, "pattern control mode", in every scene, or the other channels are ignored.
4. **Save the workspace** into `qlc/`, e.g. `qlc/show.qxw`.
5. **Upload** with the uploader app (below), or by hand with steps 6 and 7.
6. **Convert:**
   ```bash
   python3 tools/qxw_to_scenes.py qlc/show.qxw -o data/scenes.json
   ```
   This prints a summary and refuses to write anything if there are errors (duplicate buttons, button 8, address clashes, universe 2+).
7. **Flash the scenes:** `pio run -t uploadfs`. Firmware doesn't need rebuilding for scene changes.

**Power off or unplug the laser before uploading, whichever way you do it.** The box
stops sending DMX while it is being written to, and the laser's behaviour on DMX loss
is still unverified.

## Uploader app

`minidesk-uploader` is a small window for Linux, Windows and macOS: choose the `.qxw`,
check the scene list, tick "laser is off", press **Upload scenes**. It runs the same
converter, refuses to upload if there are errors, finds the box on USB by itself, and
reads the box's boot log afterwards to confirm how many scenes it loaded.
**Install firmware + scenes** sets up a new box; no PlatformIO needed.

Download it from the [releases page](https://github.com/edmonkey-nz/minidesk/releases).
The builds aren't signed, so:

- **Windows:** SmartScreen warns. Choose "More info", then "Run anyway".
- **macOS:** unzip, then right-click the app and choose Open the first time. Apple silicon only.
- **Linux:** `chmod +x minidesk-uploader-linux`. You need to be in the `dialout` group
  (`sudo usermod -aG dialout $USER`, then log out and in).

From source, or headless:

```bash
pip install -r tools/requirements-uploader.txt
python3 tools/minidesk_uploader.py                 # the app
python3 tools/minidesk_flash.py qlc/show.qxw       # same thing, command line
```

Releases are built by `.github/workflows/release.yml`: push a tag like `v1.0.0`.
To build for your own OS: `pio run -e c3 && python3 tools/build_uploader.py` (needs `pyinstaller`).

The display labels are the first 6 characters after the number. Seven-segment can't draw some letters well (M, W, K, X, V), so choose labels you can read.

## OLED status screen

The C3 board's little OLED shows what the box is doing, at 10 frames a second:

```
 1 Eyes            <- scene name; white bar = a scene is live. Otherwise BLANK or a status
 Dual head Laser   <- fixture (scrolls if long; cycles if there are several)
 33-42       40Hz  <- its DMX addresses, and DMX frames per second
 ▌▌▌▌▌▌▌ _ _ _ ::  <- live value of each channel as it goes out | held buttons
               -=- <-                                          | TX: sweeps while sending
```

Fixture names come from the QLC+ workspace, so re-convert older `scenes.json` files to
get them. Without a fixture list it shows 12 channels from the first non-zero one.

## Boot messages

| Display | Meaning |
|---|---|
| `boot` | starting |
| `3 SCEnES` | loaded 3 scenes |
| `3 ok 1Er` | 3 loaded, 1 rejected (see serial log for which) |
| `0 SCEnES` | file is valid but has no scenes |
| `no FILE` | no `/scenes.json`: run `pio run -t uploadfs` |
| `JSon Err` / `FMt Err` | file is corrupt or the wrong format version |
| `FS Err` | LittleFS didn't mount (filesystem never flashed?) |

Every one of these leaves the box in BLANK.

## Runtime messages

| Display | Meaning | Output |
|---|---|---|
| `bLAnK` | BLANK active | all channels 0 |
| `1 Tunnel` | scene 1 active | scene 1 |
| `4 EMPty` | button 4 has no scene | unchanged |
| `PAnEL Er` | button reads look like a disconnected panel | BLANK after 0.5 s |
| `dMX Err` | no DMX frame started for 200 ms | — |
| `dMX init` | UART driver failed to start | — |

The decimal point on the last digit and the onboard LED blink only while DMX frames are actually going out.

## Bench test before trusting it

1. Send a scene from QLC+ through the USB-DMX adaptor and note exactly what the laser does.
2. Send the same scene from the box. The behaviour should be identical.
3. Run a scene for an hour, ideally one with lots of channels at 255, and watch for glitches or jumps to auto mode. If you see any, swap the RS485 module for a MAX3485 with DE tied high.
4. Check that BLANK really blanks.
5. Unplug the DMX cable while a scene is running and note what the laser does. Record it in `CLAUDE.md`.

## Development

```bash
pio run && pio run -t upload        # firmware
pio test -e native                  # scene parsing/debounce tests on your PC
python3 -m pytest tools/tests -q    # converter + uploader tests (pip install pytest, plus
                                    # tools/requirements-uploader.txt for the uploader ones)
```

# minidesk — hard constraints

ESP32-C3 + TM1638 "LED&KEY" panel (8 buttons, 8 LEDs, 8× 7-seg) + TTL→RS485 module.
Recalls static DMX scenes on any fixtures patched on universe 1. Scenes are authored
in QLC+ and converted to `data/scenes.json` by `tools/qxw_to_scenes.py`. Firmware and
converter are fixture-agnostic on purpose: per-fixture knowledge lives in
"Fixture notes" below and in the README, never in code.

Use NZ/British spelling in code comments, docs and UI text.

## Output safety (non-negotiable)

Write every rule as if the output drives a laser: the box was built for one, and a
laser is the worst case. These rules override "never fail dark" from general
practice: the safe failure state here is **BLANK**.

1. **Boot into BLANK.** The DMX pin is driven high (idle) before anything else in
   `setup()`, and the first frame sent is BLANK.
2. **BLANK = every slot 0.** That is dark for the fixtures tested so far (see
   "Fixture notes") and for most dimmers and PARs, but not universally: a moving head
   homes its pan/tilt, and some fixtures pick an auto or sound mode at 0. Check each
   new fixture; don't add per-fixture BLANK values to the firmware.
3. **Button 8 is hard-wired BLANK.** It is never assignable to a scene, in firmware
   (`kBlankButton`) or converter (`BLANK_BUTTON`). BLANK wins over any other
   simultaneous press, and while BLANK is held a scene press re-asserts BLANK
   (`pickAction()` takes the held mask for this).
4. **Invalid scenes are rejected whole, never partially applied.** A half-applied
   scene is an undefined state.
5. **Any load failure → zero scenes → BLANK only**, with the reason on the display
   and serial. Never auto-format LittleFS.
6. **Panel fault → BLANK.** If button reads look like a disconnected panel
   (> 2 bits set) for `PANEL_FAULT_BLANK_MS`, go BLANK, because the operator can no
   longer reach the BLANK button.
7. **Hung loop → watchdog reboot → BLANK.** Keep `enableLoopWDT()`.
8. Pressing an empty scene button changes **nothing** on the output.

## Never block

- DMX must stream continuously (~40 Hz) from boot. The only busy-wait allowed in
  `loop()` is BREAK + MAB (~0.2 ms) in `DmxTx::service()`.
- No `delay()` in `loop()`. No polling loops waiting on hardware.
- Serial is USB CDC with `setTxTimeoutMs(0)`; log on events only, never per frame.
- **OLED:** the visible 72×40 window (5 pages, ~9 ms of I2C) is pushed only right
  after a DMX frame starts, while the UART drains the frame by interrupt. Never call
  `display()` (a full push is ~25 ms). I2C timeout is 5 ms; a missing panel is probed
  every 3 s and otherwise ignored. The boot log's `[dmx] N frames/s; slowest OLED
  push N us` line is the check: ~40 frames/s, push well under 25 ms.
- No WiFi/BLE in v1. If added later (field editing), it must not starve
  `dmx.service()`; measure frame rate before and after.

## Hardware constraints

| C3 GPIO | To | Notes |
|---|---|---|
| 3 | TM1638 STB | |
| 4 | TM1638 CLK | |
| 10 | TM1638 DIO | **10k pull-up to 3V3**. DIO is open-drain on reads. The pull-up also makes a disconnected panel read 0xFF, which the debouncer detects as a fault. |
| 7 | RS485 TXD | **10k pull-up to 3V3** so the line idles in mark during reset |
| 8 | onboard LED | active low, DMX heartbeat |
| 5/6 | onboard 0.42" OLED (I2C SDA/SCL, 0x3C) | status display, `src/oled_status.*` |

- **TM1638 panel powered from 3V3, never 5 V.** At 5 V its DIO pull-up would put
  5 V on a non-5 V-tolerant C3 pin.
- Do not put anything on GPIO 2, 8 or 9 that could be held at boot (strapping pins).
  GPIO 18/19 are USB.
- RS485 module (ePartners TP2003) is an **auto-direction** board: it only drives the
  line when TX is low; mark/idle is held by bias resistors. At 250 kbaud this is
  marginal. If output glitches, swap to a MAX3485/SP3485 breakout with DE tied
  high. No firmware change needed. RXD unconnected. Earth terminal unconnected.
- DMX: XLR pin 1 → GND, pin 2 → B−, pin 3 → A+. Swap A/B first if no response.
  Some fixtures (the Dual head Laser) use 5-pin XLR: pins 1–3 are the same as 3-pin.

## DMX driver

`src/dmx_tx.*` talks to the ESP-IDF UART driver directly (not `HardwareSerial`,
not esp_dmx). 250000 8N2, BREAK by inverting TX for 176 µs, MAB 16 µs, start code 0,
512 slots, frame interval 25 ms. Don't swap in a library without a reason that
survives the "never block" rules above.

## Scene file format v1

Shared contract between `lib/scenelogic` (firmware) and `tools/qxw_to_scenes.py`:

```json
{"version": 1, "source": "show.qxw", "scenes": [
  {"button": 1, "label": "Tunnel", "name": "1 Tunnel", "values": [[1, 64], [2, 200]]}
]}
```

- `button` 1–7. `values` = `[address 1–512, value 0–255]`, unique addresses.
  Unlisted channels are 0, so the converter drops zeros.
- `label` ≤ 6 printable ASCII chars, no `.` (it would merge into a decimal point).
- `name` (the QLC+ scene name) and the optional top-level `fixtures` list
  (`[{"name", "address" 1-based, "channels"}]`) are **display only**, for the OLED.
  The firmware skips bad fixture entries and never rejects scenes over them, and
  older firmware ignores the key, so adding it didn't need a version bump.
- **Changing the format means:** bump `kSceneFormatVersion` and `FORMAT_VERSION`
  together, update both parsers, update both test suites.
  `validate_document()` in the converter mirrors the firmware's `parseScenes()`
  rules; keep them in step.
- `tools/minidesk_flash.py` (the uploader) mirrors firmware settings too:
  `SCENES_MAX_BYTES` (include/config.h) and `LFS_CONFIG` (the Arduino-ESP32
  `CONFIG_LITTLEFS_*` sdkconfig values). If the framework's LittleFS config changes,
  the box will boot "FS Err" on uploaded images: re-check against the pio-built
  `littlefs.bin` (`tools/tests/test_minidesk_flash.py` shows how).

## OLED (0.42" SSD1306 on the C3 board)

Lessons from `../lightswing` (OLED.md there has the full story):
- **SSD1306, never SH110X.** `begin(SSD1306_SWITCHCAPVCC, 0x3C, false, false)` after
  `Wire.begin(5, 6)`. SH110X sends the wrong charge-pump command: every call
  succeeds and the screen stays black.
- **Power-cycle to test a dark panel.** A reset after flashing keeps the charge pump
  running, which hides a wrong driver until the next cold power-up.
- Geometry (`include/oled_panel.h`): 72×40 glass at controller columns 28–99, rows
  24–63 unrotated. Build with `-D OLED_INVERTED=1` if the board is mounted upside down.

## Fixture notes

Custom fixture definitions (`.qxf`) live in `qlc/fixtures/`. **Adding a fixture:**
test BLANK (button 8) and DMX loss (pull the cable mid-scene) on it, then add a
section here with the results.

### Dual head Laser (`qlc/fixtures/Dual-head-Laser2.qxf`, 10 channels)

- CH1 Run mode: 0–63 undefined (all-zero tested: off), 64–127 **pattern control
  mode — scenes must use this range or CH2–10 are ignored**, 128–191 self-walking,
  192–255 audio mode.
- BLANK (every slot 0) stops it. Tested 2026-10-16.
- On DMX signal loss it **stops**. Tested 2026-10-16. So gaps where the box sends
  nothing (flashing, a watchdog reboot, a pulled cable) leave it dark.

## Hardware verification

Per-fixture results (BLANK, DMX loss) are under "Fixture notes". The uploader's
"fixtures are safe if DMX stops" checkbox stays: not every fixture goes dark.

- [ ] RS485 auto-direction module gives glitch-free output for an hour, compared
      against the USB-DMX adaptor on the same scene. **Deferred by choice
      (2026-10-16):** fine in use so far. If output glitches or a fixture jumps to
      another mode, do this test, then swap to a MAX3485 with DE tied high.
- [x] OLED status screen on this board: geometry (borrowed from lightswing),
      orientation, and survives a cold power-up. Checked 2026-10-16; DMX holds
      40 frames/s with it running, slowest push 10.2 ms.
- [x] LED&KEY board works reliably at 3V3 (brightness acceptable). Bench-checked
      2026-10-16: all 8 buttons, display, LEDs, and DIO-pull fault detection.
- [x] ESP32-C3 build with the pioarduino platform (flashed and running 2026-10-16:
      boots BLANK, DMX streaming, LittleFS mounts).

## Commands

```bash
pio run                          # build firmware
pio run -t upload                # flash
pio run -t uploadfs              # flash data/scenes.json to LittleFS
pio device monitor               # serial log
pio test -e native               # host tests for lib/scenelogic
python3 -m pytest tools/tests -q # converter tests
python3 tools/qxw_to_scenes.py qlc/show.qxw -o data/scenes.json
python3 tools/qxw_to_scenes.py --check data/scenes.json
python3 tools/minidesk_flash.py qlc/show.qxw        # convert + upload over USB
python3 tools/minidesk_uploader.py                  # same, as an app
```

## Layout

```
src/main.cpp             panel, state, display; wiring of everything
src/dmx_tx.*             DMX transmit (ESP-only)
src/oled_status.*        0.42" OLED status screen (display only)
include/oled_panel.h     OLED glass window geometry
include/config.h         pins and tunables
lib/scenelogic/          hardware-free logic: scene parsing, debounce, display text
test/test_scenelogic/    Unity tests for scenelogic (native)
tools/qxw_to_scenes.py   QLC+ workspace -> scenes.json
tools/minidesk_flash.py  convert + write scenes (or firmware) over USB; CLI and app engine
tools/minidesk_uploader.py  tkinter app (info pane, links, "safe if DMX stops" gate)
tools/build_uploader.py  PyInstaller packaging; CI: .github/workflows/release.yml
tools/tests/             pytest + sample.qxw
qlc/                     your QLC+ workspaces (.qxw)
qlc/fixtures/            custom fixture definitions (.qxf) the workspaces use
data/scenes.json         what gets flashed to LittleFS
```

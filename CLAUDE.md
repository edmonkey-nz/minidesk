# dmx-scene-box — hard constraints

ESP32-C3 + TM1638 "LED&KEY" panel (8 buttons, 8 LEDs, 8× 7-seg) + TTL→RS485 module.
Recalls static DMX scenes on a cheap dual-head DMX laser. Scenes are authored in
QLC+ and converted to `data/scenes.json` by `tools/qxw_to_scenes.py`.

Use NZ/British spelling in code comments, docs and UI text.

## Laser safety (non-negotiable)

This drives a laser. These rules override "never fail dark" from general practice:
the safe failure state here is **BLANK**.

1. **Boot into BLANK.** The DMX pin is driven high (idle) before anything else in
   `setup()`, and the first frame sent is BLANK.
2. **BLANK = every slot 0.** Assumed to mean laser off because the fixture's CH1
   (Run mode) 0–63 is undefined in the .qxf. **Unverified on hardware** — see below.
3. **Button 8 is hard-wired BLANK.** It is never assignable to a scene, in firmware
   (`kBlankButton`) or converter (`BLANK_BUTTON`). BLANK wins over any other
   simultaneous press.
4. **Invalid scenes are rejected whole, never partially applied.** A half-applied
   laser scene is an undefined state.
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
| 5/6 | onboard 0.42" OLED (I2C) | unused in v1 |

- **TM1638 panel powered from 3V3, never 5 V.** At 5 V its DIO pull-up would put
  5 V on a non-5 V-tolerant C3 pin.
- Do not put anything on GPIO 2, 8 or 9 that could be held at boot (strapping pins).
  GPIO 18/19 are USB.
- RS485 module (ePartners TP2003) is an **auto-direction** board: it only drives the
  line when TX is low; mark/idle is held by bias resistors. At 250 kbaud this is
  marginal. If the laser glitches, swap to a MAX3485/SP3485 breakout with DE tied
  high. No firmware change needed. RXD unconnected. Earth terminal unconnected.
- DMX: XLR pin 1 → GND, pin 2 → B−, pin 3 → A+. Swap A/B first if no response.
  The laser uses 5-pin XLR (pins 1–3 same as 3-pin).

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
- **Changing the format means:** bump `kSceneFormatVersion` and `FORMAT_VERSION`
  together, update both parsers, update both test suites.
  `validate_document()` in the converter mirrors the firmware's `parseScenes()`
  rules; keep them in step.

## Fixture notes (Dual-head-Laser2.qxf, 10 channels)

- CH1 Run mode: 0–63 undefined (assumed off), 64–127 **pattern control mode —
  scenes must use this range or CH2–10 are ignored**, 128–191 self-walking,
  192–255 audio mode.
- Firmware and converter are fixture-agnostic on purpose; this rule lives here and
  in the README, not in code.

## Unverified on hardware (resolve before trusting a show)

- [ ] CH1 = 0 actually blanks the laser.
- [ ] What the laser does on DMX signal loss (hold last / blank / auto mode).
- [ ] RS485 auto-direction module gives glitch-free output for an hour, compared
      against the USB-DMX adaptor on the same scene.
- [ ] LED&KEY board works reliably at 3V3 (brightness acceptable).
- [ ] ESP32-C3 build with the pioarduino platform (scaffold was not compiled for the
      target when written; host tests and converter tests pass).

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
```

## Layout

```
src/main.cpp             panel, state, display; wiring of everything
src/dmx_tx.*             DMX transmit (ESP-only)
include/config.h         pins and tunables
lib/scenelogic/          hardware-free logic: scene parsing, debounce, display text
test/test_scenelogic/    Unity tests for scenelogic (native)
tools/qxw_to_scenes.py   QLC+ workspace -> scenes.json
tools/tests/             pytest + sample.qxw
qlc/                     fixture definition and your QLC+ workspaces
data/scenes.json         what gets flashed to LittleFS
```

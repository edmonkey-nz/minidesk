// Pin map and tunables. Pin changes must also be reflected in README.md and CLAUDE.md.
#pragma once

#include <stdint.h>
#include <stddef.h>

// ESP32-C3. Keep 2, 8, 9 (strapping) free of anything that could be held at boot.
// 5/6 are the onboard 0.42" OLED's I2C on that board variant (unused in v1).
constexpr int PIN_TM_STB = 3;
constexpr int PIN_TM_CLK = 4;
constexpr int PIN_TM_DIO = 10;   // needs 10k pull-up to 3V3 (TM1638 DIO is open-drain)
constexpr int PIN_DMX_TX = 7;    // to RS485 module TXD, 10k pull-up to 3V3

constexpr int PIN_STATUS_LED = 8;  // onboard LED, active low. -1 to disable.
constexpr bool STATUS_LED_ACTIVE_LOW = true;

// DMX
constexpr uint16_t DMX_SLOT_COUNT = 512;  // some cheap fixtures misbehave on short frames

// Panel
constexpr uint32_t BUTTON_POLL_MS = 20;
constexpr uint8_t BUTTON_STABLE_READS = 3;    // 3 x 20 ms = 60 ms debounce
constexpr uint32_t PANEL_FAULT_BLANK_MS = 500; // can't read buttons this long -> BLANK
constexpr uint8_t PANEL_BRIGHTNESS = 2;        // 0..7
constexpr uint32_t BOOT_MESSAGE_MS = 2000;
constexpr uint32_t HEARTBEAT_MS = 250;

// Scenes
constexpr const char* SCENES_PATH = "/scenes.json";
constexpr size_t SCENES_MAX_BYTES = 48 * 1024;

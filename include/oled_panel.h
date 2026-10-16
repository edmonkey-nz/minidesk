// Geometry of the C3 board's 0.42" 72x40 SSD1306, measured on hardware in the
// lightswing project (../lightswing/include/OledPanel.h). Declared to the driver as
// 128x64; only a 72x40 window of that RAM is glass.
//
// Unrotated, the window is controller columns 28-99 and rows 24-63. Column 28 is
// measured, not assumed: an offset of 26 lost the first two pixel columns.
// Rotated 180°, Adafruit GFX maps logical (x, y) to controller (127 - x, 63 - y),
// which puts the same window at logical x 28-99, y 0-39. ROTATION and the offsets
// always change together, hence one switch. Build with -D OLED_INVERTED=1 if the
// box is built with the board upside down.
#pragma once

#include <stdint.h>

#ifndef OLED_INVERTED
#define OLED_INVERTED 0
#endif

namespace oled_panel {
constexpr int16_t WIDTH = 128;
constexpr int16_t HEIGHT = 64;
constexpr uint8_t ROTATION = OLED_INVERTED ? 2 : 0;  // 180° or none
constexpr int16_t OFFSET_X = 28;                     // same either way: 28 centres 72 in 128
constexpr int16_t OFFSET_Y = OLED_INVERTED ? 0 : 24;
constexpr int16_t VISIBLE_WIDTH = 72;
constexpr int16_t VISIBLE_HEIGHT = 40;

// The same window in controller RAM terms, for pushing only the glass: columns
// 28-99, pages 3-7 (rows 24-63). Rotation is applied when drawing, so the buffer is
// already in controller order. 360 bytes instead of 1024, about 2 ms per page.
constexpr uint8_t RAM_COL_START = 28;
constexpr uint8_t RAM_PAGE_START = 3;
constexpr uint8_t RAM_PAGES = VISIBLE_HEIGHT / 8;
}  // namespace oled_panel

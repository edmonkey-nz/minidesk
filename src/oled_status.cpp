#include "oled_status.h"

#include <Wire.h>
#include <scenelogic.h>
#include <stdio.h>
#include <string.h>

#include "config.h"
#include "oled_panel.h"

using namespace oled_panel;

namespace {
constexpr int16_t kCharW = 6;           // default GFX font, incl. 1 px spacing
constexpr uint8_t kMarqueePause = 10;   // ticks (draws) to pause at each end
constexpr int16_t kBarsW = 60;          // bars use x 0-59, IO column x 61-71
constexpr int16_t kBarsBase = 39;       // baseline row; bars grow up from 38
constexpr uint8_t kBarMaxH = 9;
constexpr uint8_t kMaxBars = 12;
constexpr int16_t kIoX = 61;
}  // namespace

// clkAfter 400 kHz too, so our own page writes run at full speed.
OledStatus::OledStatus() : panel_(WIDTH, HEIGHT, &Wire, -1, OLED_I2C_HZ, OLED_I2C_HZ) {}

void OledStatus::begin(uint32_t nowMs) {
  lastRetryMs_ = nowMs;
  ok_ = tryInit();
  Serial.printf("[oled] %s\n", ok_ ? "ok" : "not found at 0x3C, retrying every few seconds");
}

// Probe before begin(): calling begin() with no panel on the bus leaves the driver
// half-configured (lightswing). No full display() push here: the off-glass RAM is
// never visible, and a full push is ~25 ms of I2C.
bool OledStatus::tryInit() {
  Wire.beginTransmission(OLED_I2C_ADDR);
  if (Wire.endTransmission() != 0) return false;
  // SSD1306_SWITCHCAPVCC turns on the charge pump; without it the panel ACKs
  // everything and stays dark after a cold power-up. periphBegin=false: Wire is
  // already on our pins, and the library would re-begin it on the default ones.
  if (!panel_.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR, false, false)) return false;
  panel_.setRotation(ROTATION);
  panel_.setTextWrap(false);
  panel_.setTextSize(1);
  lastDrawMs_ = 0;
  return true;
}

void OledStatus::service(uint32_t nowMs, bool frameJustStarted, const OledView& v) {
  if (!ok_) {
    if (nowMs - lastRetryMs_ >= OLED_RETRY_MS) {
      lastRetryMs_ = nowMs;
      if ((ok_ = tryInit())) Serial.println("[oled] ok");
    }
    return;
  }

  const uint32_t since = nowMs - lastDrawMs_;
  if (since < OLED_DRAW_MS) return;
  // Wait for the start of a DMX frame, unless frames have stopped altogether.
  if (!frameJustStarted && since < 3 * OLED_DRAW_MS) return;
  lastDrawMs_ = nowMs;
  tick_++;

  render(v);
  const uint32_t t0 = micros();
  for (uint8_t p = 0; p < RAM_PAGES; ++p) {
    if (!sendPage(p)) {
      ok_ = false;
      lastRetryMs_ = nowMs;
      Serial.println("[oled] lost, retrying");
      return;
    }
  }
  const uint32_t us = micros() - t0;
  if (us > maxPushUs_) maxPushUs_ = us;
}

// One page of the visible window: set the column and page range, then 72 bytes of
// data in one transaction (fits the 128-byte Wire buffer). Relies on the horizontal
// addressing mode Adafruit's begin() sets. From lightswing.
bool OledStatus::sendPage(uint8_t page) {
  const uint8_t ramPage = RAM_PAGE_START + page;
  Wire.beginTransmission(OLED_I2C_ADDR);
  Wire.write(0x00);  // command stream
  Wire.write(0x21);  // column address range
  Wire.write(RAM_COL_START);
  Wire.write(RAM_COL_START + VISIBLE_WIDTH - 1);
  Wire.write(0x22);  // page address range
  Wire.write(ramPage);
  Wire.write(ramPage);
  if (Wire.endTransmission() != 0) return false;

  const uint8_t* row = panel_.getBuffer() + ramPage * WIDTH + RAM_COL_START;
  Wire.beginTransmission(OLED_I2C_ADDR);
  Wire.write(0x40);  // data stream
  Wire.write(row, VISIBLE_WIDTH);
  return Wire.endTransmission() == 0;
}

void OledStatus::text(int16_t x, int16_t y, const char* s, uint16_t colour) {
  panel_.setTextColor(colour);
  panel_.setCursor(OFFSET_X + x, OFFSET_Y + y);
  panel_.print(s);
}

// Text wider than the window scrolls. What spills past the window lands in
// controller RAM that isn't glass, so it needs no clipping.
void OledStatus::marquee(int16_t y, const char* s, uint16_t colour) {
  const uint16_t w = static_cast<uint16_t>(strlen(s) * kCharW);
  const int16_t x = scenelogic::marqueeOffset(w > 0 ? w - 1 : 0, VISIBLE_WIDTH, tick_, kMarqueePause);
  text(x, y, s, colour);
}

void OledStatus::render(const OledView& v) {
  panel_.clearDisplay();
  const auto fill = [&](int16_t x, int16_t y, int16_t w, int16_t h) {
    panel_.fillRect(OFFSET_X + x, OFFSET_Y + y, w, h, SSD1306_WHITE);
  };

  // Row 1: scene or status. Inverted while a scene is live, so "output on" reads
  // at a glance.
  if (v.live) fill(0, 0, VISIBLE_WIDTH, 9);
  marquee(1, v.title, v.live ? SSD1306_BLACK : SSD1306_WHITE);

  // Row 2: fixture name.
  marquee(11, v.fixture[0] ? v.fixture : "no fixture list", SSD1306_WHITE);

  // Row 3: addresses and frame rate.
  char buf[16];
  if (v.channelCount) {
    snprintf(buf, sizeof buf, "%u-%u", v.firstAddress, v.firstAddress + v.channelCount - 1);
    text(0, 21, buf, SSD1306_WHITE);
  }
  snprintf(buf, sizeof buf, "%uHz", v.framesPerSec);
  text(VISIBLE_WIDTH - static_cast<int16_t>(strlen(buf)) * kCharW + 1, 21, buf, SSD1306_WHITE);

  // Bars: what is actually on the wire for this fixture's channels.
  const uint16_t n = v.channelCount < kMaxBars ? v.channelCount : kMaxBars;
  if (n && v.slots) {
    const int16_t pitch = kBarsW / n;
    const int16_t w = pitch > 1 ? pitch - 1 : 1;
    for (uint16_t i = 0; i < n; ++i) {
      const int16_t x = static_cast<int16_t>(i) * pitch;
      fill(x, kBarsBase, w, 1);
      const uint8_t h = scenelogic::barHeight(v.slots[v.firstAddress - 1 + i], kBarMaxH);
      if (h) fill(x, kBarsBase - h, w, h);
    }
  }

  // IO column. Buttons 1-4 over 5-8: a 2x2 dot while held, a pixel otherwise;
  // all flashing on a panel fault.
  for (uint8_t b = 0; b < 8; ++b) {
    const int16_t x = kIoX + (b % 4) * 3;
    const int16_t y = 30 + (b / 4) * 3;
    const bool on = v.panelFault ? (tick_ & 2) : (v.heldButtons & (1u << b));
    if (on) {
      fill(x, y, 2, 2);
    } else if (!v.panelFault) {
      fill(x, y, 1, 1);
    }
  }
  // TX: a block that sweeps back and forth while frames go out, and stops if they stop.
  const uint32_t pos = (v.frames / 4) % 16;
  const int16_t p = static_cast<int16_t>(pos < 9 ? pos : 16 - pos);
  fill(kIoX, kBarsBase, 11, 1);
  fill(kIoX + p, kBarsBase - 2, 3, 2);
}

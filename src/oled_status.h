// Status screen on the C3 board's 0.42" OLED (72x40 visible).
//
//   +------------------------+
//   | 1 Eyes                 |  scene name; inverted while a scene is live, else BLANK/status
//   | Dual head Laser        |  fixture name (scrolls if long)
//   | 33-42           40Hz   |  its DMX addresses, DMX frame rate
//   | ||| |  |||  ||  :: ::  |  live channel values as bars | held buttons
//   | ________________ -=-   |  (baseline per channel)      | TX activity
//   +------------------------+
//
// Display only: nothing here affects DMX output. Never blocks DMX: the window is
// pushed (5 pages, ~9 ms of I2C) right after a DMX frame starts, while the UART
// drains the frame by interrupt, so it can't delay the next frame. A missing or
// failed panel is retried every few seconds and otherwise ignored.
#pragma once

#include <Adafruit_SSD1306.h>
#include <stdint.h>

struct OledView {
  const char* title = "";        // scene name or status
  bool live = false;             // a scene is on the output: draw the title inverted
  const char* fixture = "";      // "" if the scenes file has no fixture list
  uint16_t firstAddress = 1;     // bars show these channels
  uint16_t channelCount = 0;
  const uint8_t* slots = nullptr;  // DMX slots, [0] = address 1
  uint16_t framesPerSec = 0;
  uint32_t frames = 0;           // drives the TX activity marker
  uint8_t heldButtons = 0;       // bit0 = button 1
  bool panelFault = false;
};

class OledStatus {
 public:
  OledStatus();

  // Wire must already be begun on the OLED pins. Never waits for the panel.
  void begin(uint32_t nowMs);

  // Call every loop. Draws at most every drawIntervalMs, right after a DMX frame
  // starts (or regardless, if frames have stopped).
  void service(uint32_t nowMs, bool frameJustStarted, const OledView& v);

  bool ok() const { return ok_; }
  uint32_t maxPushUs() const { return maxPushUs_; }

 private:
  bool tryInit();
  bool sendPage(uint8_t page);
  void render(const OledView& v);
  void text(int16_t x, int16_t y, const char* s, uint16_t colour);
  void marquee(int16_t y, const char* s, uint16_t colour);

  Adafruit_SSD1306 panel_;
  bool ok_ = false;
  uint32_t lastRetryMs_ = 0;
  uint32_t lastDrawMs_ = 0;
  uint32_t tick_ = 0;
  uint32_t maxPushUs_ = 0;
};

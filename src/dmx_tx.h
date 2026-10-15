// Transmit-only DMX512 on an ESP32 UART, using the IDF driver directly.
//
// Frame: BREAK (TX inverted, ~176 us) -> MAB (~16 us) -> start code 0 -> slots.
// service() is non-blocking apart from the break/MAB busy-wait (~0.2 ms):
// the slot data goes into the UART driver's TX ring buffer and an ISR drains it.
#pragma once

#include <Arduino.h>
#include <driver/uart.h>

class DmxTx {
 public:
  static constexpr uint32_t kBaud = 250000;
  static constexpr uint32_t kBreakUs = 176;          // spec min 92
  static constexpr uint32_t kMabUs = 16;             // spec min 12
  static constexpr uint32_t kFrameIntervalUs = 25000; // ~40 Hz with 512 slots
  static constexpr uint32_t kStallUs = 200000;       // no frame started for this long = stalled

  bool begin(int txPin, uart_port_t port, uint16_t slots);

  // Copy `count` slot values (address 1 first). Slots beyond count are zeroed.
  // Safe to call at any time; takes effect on the next frame.
  void setFrame(const uint8_t* values, uint16_t count);

  // Call as often as possible from loop(). Returns true if a frame was started.
  bool service();

  bool ok() const { return ok_; }
  bool stalled() const;
  uint32_t framesSent() const { return frames_; }

 private:
  uart_port_t port_ = UART_NUM_1;
  uint16_t slots_ = 512;
  uint8_t buf_[513] = {};  // [0] = start code, always 0
  uint32_t lastStartUs_ = 0;
  uint32_t frames_ = 0;
  bool started_ = false;
  bool ok_ = false;
};

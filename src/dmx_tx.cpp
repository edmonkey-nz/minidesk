#include "dmx_tx.h"

#include <esp_idf_version.h>
#include <string.h>

bool DmxTx::begin(int txPin, uart_port_t port, uint16_t slots) {
  port_ = port;
  slots_ = slots < 24 ? 24 : (slots > 512 ? 512 : slots);

  uart_config_t cfg = {};
  cfg.baud_rate = static_cast<int>(kBaud);
  cfg.data_bits = UART_DATA_8_BITS;
  cfg.parity = UART_PARITY_DISABLE;
  cfg.stop_bits = UART_STOP_BITS_2;
  cfg.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
#if ESP_IDF_VERSION_MAJOR >= 5
  cfg.source_clk = UART_SCLK_DEFAULT;
#else
  cfg.source_clk = UART_SCLK_APB;
#endif

  // RX buffer must exceed the hardware FIFO even though we never receive.
  // TX ring buffer holds a whole frame so uart_write_bytes() returns at once.
  ok_ = uart_driver_install(port_, 256, 1024, 0, nullptr, 0) == ESP_OK &&
        uart_param_config(port_, &cfg) == ESP_OK &&
        uart_set_pin(port_, txPin, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) == ESP_OK;
  return ok_;
}

void DmxTx::setFrame(const uint8_t* values, uint16_t count) {
  if (count > 512) count = 512;
  memcpy(&buf_[1], values, count);
  if (count < 512) memset(&buf_[1 + count], 0, 512 - count);
}

bool DmxTx::service() {
  if (!ok_) return false;
  const uint32_t now = micros();
  if (started_ && (now - lastStartUs_) < kFrameIntervalUs) return false;
  // Previous frame must be fully on the wire (incl. stop bits) before the break.
  if (started_ && uart_wait_tx_done(port_, 0) != ESP_OK) return false;

  // Interrupts can only lengthen BREAK and MAB, which the spec allows.
  uart_set_line_inverse(port_, UART_SIGNAL_TXD_INV);
  delayMicroseconds(kBreakUs);
  uart_set_line_inverse(port_, UART_SIGNAL_INV_DISABLE);
  delayMicroseconds(kMabUs);
  uart_write_bytes(port_, reinterpret_cast<const char*>(buf_), slots_ + 1u);

  lastStartUs_ = now;
  started_ = true;
  frames_++;
  return true;
}

bool DmxTx::stalled() const {
  if (!ok_) return true;
  if (!started_) return false;
  return (micros() - lastStartUs_) > kStallUs;
}

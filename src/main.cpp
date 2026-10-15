// dmx-scene-box: recall static DMX scenes on a laser from a TM1638 LED&KEY panel.
//
// Safety model (see CLAUDE.md): boot into BLANK, stream DMX continuously,
// button 8 is always BLANK, anything unexpected -> BLANK and say why.

#include <Arduino.h>
#include <LittleFS.h>
#include <TM1638plus.h>
#include <scenelogic.h>

#include "config.h"
#include "dmx_tx.h"

using namespace scenelogic;

static DmxTx dmx;
static TM1638plus panel(PIN_TM_STB, PIN_TM_CLK, PIN_TM_DIO, true /* high-freq MCU */);
static SceneSet sceneSet;  // ~3.6 KB: static on purpose, not on the stack
static ButtonDebouncer debouncer(BUTTON_STABLE_READS);
static LoadReport loadReport;

static const uint8_t kBlankFrame[kDmxSlots] = {};
static uint8_t activeButton = kBlankButton;

// Panel fault tracking
static bool panelFault = false;
static uint32_t panelFaultSinceMs = 0;

// Temporary message overlay
static char msgText[9] = "";
static uint32_t msgStartMs = 0;
static uint32_t msgDurationMs = 0;

// What is currently on the panel, so we only write on change
static char shownText[9] = "";
static bool shownDp = false;
static int16_t shownLeds = -1;

// ---------------------------------------------------------------------------

static void statusLed(bool on) {
  if (PIN_STATUS_LED < 0) return;
  digitalWrite(PIN_STATUS_LED, (on != STATUS_LED_ACTIVE_LOW) ? HIGH : LOW);
}

static void showMessage(const char* text, uint32_t durationMs) {
  formatStatus(msgText, text);
  msgStartMs = millis();
  msgDurationMs = durationMs;
}

static void goBlank(const char* reason) {
  activeButton = kBlankButton;
  dmx.setFrame(kBlankFrame, kDmxSlots);
  Serial.printf("[scene] BLANK (%s)\n", reason);
}

static void goScene(uint8_t button) {
  const Scene& s = sceneSet.scenes[button];
  if (!s.present) {
    char m[9];
    snprintf(m, sizeof m, "%u EMPty", button);
    showMessage(m, 1200);
    Serial.printf("[scene] button %u has no scene, output unchanged\n", button);
    return;
  }
  activeButton = button;
  dmx.setFrame(s.frame, kDmxSlots);
  Serial.printf("[scene] %u '%s'\n", button, s.label);
}

static void loadScenes() {
  loadReport = LoadReport{};

  if (!LittleFS.begin(false)) {  // never auto-format: a mount failure should be visible
    loadReport.status = LoadStatus::FsError;
    return;
  }
  File f = LittleFS.open(SCENES_PATH, "r");
  if (!f) {
    loadReport.status = LoadStatus::NoFile;
    return;
  }
  const size_t size = f.size();
  if (size == 0 || size > SCENES_MAX_BYTES) {
    f.close();
    loadReport.status = LoadStatus::BadFormat;
    return;
  }
  char* buf = static_cast<char*>(malloc(size));
  if (!buf) {
    f.close();
    loadReport.status = LoadStatus::FsError;
    return;
  }
  const size_t got = f.read(reinterpret_cast<uint8_t*>(buf), size);
  f.close();
  loadReport = (got == size) ? parseScenes(buf, size, sceneSet) : LoadReport{LoadStatus::FsError, 0, 0};
  free(buf);
}

static const char* loadStatusText(char out[9]) {
  switch (loadReport.status) {
    case LoadStatus::Ok:
      if (loadReport.rejected) {
        snprintf(out, 9, "%u ok %uEr", loadReport.loaded, loadReport.rejected);
      } else {
        snprintf(out, 9, "%u SCEnES", loadReport.loaded);
      }
      break;
    case LoadStatus::Empty:      snprintf(out, 9, "0 SCEnES"); break;
    case LoadStatus::ParseError: snprintf(out, 9, "JSon Err"); break;
    case LoadStatus::BadFormat:  snprintf(out, 9, "FMt Err"); break;
    case LoadStatus::NoFile:     snprintf(out, 9, "no FILE"); break;
    case LoadStatus::FsError:    snprintf(out, 9, "FS Err"); break;
  }
  return out;
}

static void logScenes() {
  char s[9];
  Serial.printf("[load] %s (loaded %u, rejected %u)\n", loadStatusText(s), loadReport.loaded,
                loadReport.rejected);
  for (uint8_t b = 1; b <= kMaxSceneButton; ++b) {
    const Scene& sc = sceneSet.scenes[b];
    if (!sc.present) continue;
    uint16_t nonZero = 0;
    for (uint16_t i = 0; i < kDmxSlots; ++i) nonZero += sc.frame[i] ? 1 : 0;
    Serial.printf("[load]   %u '%s' %u non-zero channels\n", b, sc.label, nonZero);
  }
}

// ---------------------------------------------------------------------------

static void pollPanel(uint32_t now) {
  const uint8_t raw = panel.readButtons();
  const uint8_t pressed = debouncer.update(raw);

  if (debouncer.faultStreak() > 0) {
    if (!panelFault) {
      panelFault = true;
      panelFaultSinceMs = now;
      Serial.printf("[panel] fault: raw buttons 0x%02X\n", raw);
    } else if (activeButton != kBlankButton && now - panelFaultSinceMs >= PANEL_FAULT_BLANK_MS) {
      goBlank("panel fault");  // can't read BLANK any more, so go there ourselves
    }
    return;
  }
  if (panelFault) {
    panelFault = false;
    Serial.println("[panel] fault cleared (staying in current state)");
  }

  const uint8_t action = pickAction(pressed);
  if (action == kBlankButton) {
    goBlank("button");
  } else if (action) {
    goScene(action);
  }
}

static void writePanel(const char* text, bool dp, uint8_t leds) {
  if (strncmp(text, shownText, 8) != 0 || dp != shownDp) {
    for (uint8_t i = 0; i < 8; ++i) {
      const bool d = (i == 7) && dp;
      panel.displayASCII(i, static_cast<uint8_t>(text[i]), d ? TM1638plus::DecPointOn : TM1638plus::DecPointOff);
    }
    memcpy(shownText, text, 8);
    shownText[8] = '\0';
    shownDp = dp;
  }
  if (shownLeds != leds) {
    panel.setLEDs(leds);  // bit0 = LED1 (left)
    shownLeds = leds;
  }
}

static void updateIndicators(uint32_t now) {
  // Heartbeat: blinks only while DMX frames are actually going out.
  static uint32_t lastBeatMs = 0;
  static uint32_t lastFrames = 0;
  static bool beat = false;
  if (now - lastBeatMs >= HEARTBEAT_MS) {
    lastBeatMs = now;
    const uint32_t f = dmx.framesSent();
    beat = (f != lastFrames) ? !beat : false;
    lastFrames = f;
    statusLed(beat);
  }

  static bool stallLogged = false;
  const bool stalled = dmx.stalled();
  if (stalled != stallLogged) {
    Serial.println(stalled ? "[dmx] STALLED" : "[dmx] running");
    stallLogged = stalled;
  }

  char text[9];
  if (!dmx.ok()) {
    formatStatus(text, "dMX init");
  } else if (stalled) {
    formatStatus(text, "dMX Err");
  } else if (panelFault) {
    formatStatus(text, "PAnEL Er");
  } else if (now - msgStartMs < msgDurationMs) {
    memcpy(text, msgText, 9);
  } else if (activeButton == kBlankButton) {
    formatStatus(text, "bLAnK");
  } else {
    formatSceneText(text, activeButton, sceneSet.scenes[activeButton].label);
  }

  const uint8_t leds = static_cast<uint8_t>(1u << (activeButton - 1));
  writePanel(text, beat, leds);
}

// ---------------------------------------------------------------------------

void setup() {
  // 1. Hold the DMX line at idle (mark) before anything else can glitch it.
  pinMode(PIN_DMX_TX, OUTPUT);
  digitalWrite(PIN_DMX_TX, HIGH);

  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
  Serial.setTxTimeoutMs(0);  // never block the loop when no host is reading
#endif

  if (PIN_STATUS_LED >= 0) {
    pinMode(PIN_STATUS_LED, OUTPUT);
    statusLed(false);
  }

  // 2. DMX up and a BLANK frame on the wire as early as possible.
  dmx.begin(PIN_DMX_TX, UART_NUM_1, DMX_SLOT_COUNT);
  goBlank("boot");
  dmx.service();
  Serial.printf("[dmx] %s, %u slots\n", dmx.ok() ? "ok" : "INIT FAILED", DMX_SLOT_COUNT);

  // 3. Panel.
  panel.displayBegin();
  panel.brightness(PANEL_BRIGHTNESS);
  writePanel("boot    ", false, 0);

  // 4. Scenes. Any failure leaves zero scenes loaded; we stay BLANK either way.
  loadScenes();
  logScenes();
  char s[9];
  showMessage(loadStatusText(s), BOOT_MESSAGE_MS);

  enableLoopWDT();  // a hung loop reboots into BLANK
}

void loop() {
  const uint32_t now = millis();
  dmx.service();

  static uint32_t lastPollMs = 0;
  if (now - lastPollMs >= BUTTON_POLL_MS) {
    lastPollMs = now;
    pollPanel(now);
  }

  dmx.service();
  updateIndicators(now);
}

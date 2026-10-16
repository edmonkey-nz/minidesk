// scenelogic: everything in minidesk that doesn't touch hardware.
// Built for both the ESP32-C3 and the host (pio test -e native), so keep it
// free of Arduino.h and anything ESP-specific.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace scenelogic {

constexpr uint16_t kDmxSlots = 512;
constexpr uint8_t kNumButtons = 8;
constexpr uint8_t kBlankButton = 8;       // 1-based. Hard-wired BLANK, never a scene.
constexpr uint8_t kMaxSceneButton = 7;    // scenes live on buttons 1..7
constexpr uint8_t kLabelMax = 6;          // "1 " + 6 chars fills the 8-digit display
constexpr uint8_t kMaxSimultaneousPresses = 2;  // more than this = panel fault
constexpr int kSceneFormatVersion = 1;    // must match tools/qxw_to_scenes.py
constexpr uint8_t kNameMax = 24;          // scene and fixture names, for the OLED
constexpr uint8_t kMaxFixtures = 8;

struct Scene {
  bool present = false;
  char label[kLabelMax + 1] = {};
  char name[kNameMax + 1] = {};   // full QLC+ scene name, display only
  uint8_t frame[kDmxSlots] = {};  // frame[0] = DMX address 1. Unset channels are 0.
};

// Patched fixture, from the optional "fixtures" list. Display only: it never
// changes what is sent.
struct FixtureInfo {
  char name[kNameMax + 1] = {};
  uint16_t address = 0;   // 1-based start address
  uint16_t channels = 0;  // address + channels - 1 <= 512
};

struct SceneSet {
  Scene scenes[kMaxSceneButton + 1];  // index 1..7 used, 0 unused
  uint8_t loaded = 0;
  FixtureInfo fixtures[kMaxFixtures];
  uint8_t fixtureCount = 0;
  void clear();
};

enum class LoadStatus : uint8_t {
  Ok,          // at least one scene loaded
  Empty,       // valid file, no usable scenes
  ParseError,  // not valid JSON
  BadFormat,   // valid JSON, wrong version or shape
  NoFile,      // set by the caller, never by parseScenes()
  FsError,     // set by the caller, never by parseScenes()
};

struct LoadReport {
  LoadStatus status = LoadStatus::BadFormat;
  uint8_t loaded = 0;
  uint8_t rejected = 0;  // scene entries in the file that failed validation
};

// Parse scenes.json (format v1). Invalid scenes are rejected whole, never
// partially applied. `out` is always cleared first, so on any failure it holds
// zero scenes and the caller stays in BLANK.
LoadReport parseScenes(const char* json, size_t len, SceneSet& out);

// Keep printable ASCII (no '.'), truncate to kLabelMax.
void sanitiseLabel(const char* in, char out[kLabelMax + 1]);

// Keep printable ASCII, truncate to kNameMax.
void sanitiseName(const char* in, char out[kNameMax + 1]);

// Edge-detecting debouncer for the TM1638 button byte (bit0 = S1 ... bit7 = S8).
class ButtonDebouncer {
 public:
  explicit ButtonDebouncer(uint8_t stableReads = 3) : need_(stableReads ? stableReads : 1) {}

  // Feed one raw read. Returns a mask of buttons that became pressed this call.
  // Reads with more than kMaxSimultaneousPresses bits set are treated as a
  // panel fault (a disconnected DIO line pulled high reads 0xFF) and ignored.
  uint8_t update(uint8_t raw);

  uint8_t held() const { return stable_; }
  uint8_t faultStreak() const { return faultStreak_; }  // consecutive faulty reads

 private:
  uint8_t need_;
  uint8_t candidate_ = 0;
  uint8_t stable_ = 0;
  uint8_t count_ = 0;
  uint8_t faultStreak_ = 0;
};

// Which button to act on: 1..8, or 0 for none. `pressedMask` is this read's new
// presses, `heldMask` everything currently held (ButtonDebouncer::held()).
// BLANK always wins, including while it is held: any press with BLANK down
// re-asserts BLANK, so brushing a scene button mid-panic can't start a scene.
// Otherwise the lowest-numbered new press.
uint8_t pickAction(uint8_t pressedMask, uint8_t heldMask);

// Exactly 8 display characters + NUL.
void formatSceneText(char out[9], uint8_t button, const char* label);
void formatStatus(char out[9], const char* text);

uint8_t popcount8(uint8_t v);

// --- OLED helpers ---

// Horizontal offset (<= 0) for text `textPx` wide in a `windowPx` window, so long
// text pauses, scrolls 1 px per tick, pauses, then jumps back. 0 if it fits.
int16_t marqueeOffset(uint16_t textPx, uint16_t windowPx, uint32_t tick, uint8_t pauseTicks);

// Bar height for a DMX value, 0..maxPx. Any non-zero value gets at least 1 px.
uint8_t barHeight(uint8_t value, uint8_t maxPx);

}  // namespace scenelogic

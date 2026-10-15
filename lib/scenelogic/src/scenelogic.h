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

struct Scene {
  bool present = false;
  char label[kLabelMax + 1] = {};
  uint8_t frame[kDmxSlots] = {};  // frame[0] = DMX address 1. Unset channels are 0.
};

struct SceneSet {
  Scene scenes[kMaxSceneButton + 1];  // index 1..7 used, 0 unused
  uint8_t loaded = 0;
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
// re-asserts BLANK, so brushing a scene button mid-panic can't fire the laser.
// Otherwise the lowest-numbered new press.
uint8_t pickAction(uint8_t pressedMask, uint8_t heldMask);

// Exactly 8 display characters + NUL.
void formatSceneText(char out[9], uint8_t button, const char* label);
void formatStatus(char out[9], const char* text);

uint8_t popcount8(uint8_t v);

}  // namespace scenelogic

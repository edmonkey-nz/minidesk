#include "scenelogic.h"

#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>

namespace scenelogic {

void SceneSet::clear() {
  for (auto& s : scenes) {
    s.present = false;
    s.label[0] = '\0';
    s.name[0] = '\0';
    memset(s.frame, 0, sizeof(s.frame));
  }
  loaded = 0;
  for (auto& f : fixtures) f = FixtureInfo{};
  fixtureCount = 0;
}

uint8_t popcount8(uint8_t v) {
  uint8_t n = 0;
  while (v) {
    n += v & 1u;
    v >>= 1;
  }
  return n;
}

void sanitiseLabel(const char* in, char out[kLabelMax + 1]) {
  uint8_t n = 0;
  if (in) {
    for (const char* p = in; *p && n < kLabelMax; ++p) {
      const unsigned char c = static_cast<unsigned char>(*p);
      if (c < 0x20 || c > 0x7E || c == '.') continue;  // '.' would merge into the decimal point
      out[n++] = static_cast<char>(c);
    }
  }
  out[n] = '\0';
}

void sanitiseName(const char* in, char out[kNameMax + 1]) {
  uint8_t n = 0;
  if (in) {
    for (const char* p = in; *p && n < kNameMax; ++p) {
      const unsigned char c = static_cast<unsigned char>(*p);
      if (c < 0x20 || c > 0x7E) continue;
      out[n++] = static_cast<char>(c);
    }
  }
  out[n] = '\0';
}

namespace {

// Validate one scene object into `tmp`. Returns the button (1..7) or 0 if invalid.
uint8_t loadOne(JsonObjectConst obj, Scene& tmp) {
  JsonVariantConst b = obj["button"];
  if (!b.is<int>()) return 0;
  const int button = b.as<int>();
  if (button < 1 || button > kMaxSceneButton) return 0;  // also rejects BLANK (8)

  if (!obj["values"].is<JsonArrayConst>()) return 0;
  JsonArrayConst values = obj["values"].as<JsonArrayConst>();

  uint8_t seen[kDmxSlots / 8] = {};
  for (JsonVariantConst pairV : values) {
    if (!pairV.is<JsonArrayConst>()) return 0;
    JsonArrayConst pair = pairV.as<JsonArrayConst>();
    if (pair.size() != 2) return 0;
    if (!pair[0].is<int>() || !pair[1].is<int>()) return 0;
    const int addr = pair[0].as<int>();
    const int val = pair[1].as<int>();
    if (addr < 1 || addr > kDmxSlots || val < 0 || val > 255) return 0;
    const int i = addr - 1;
    if (seen[i / 8] & (1u << (i % 8))) return 0;  // duplicate address
    seen[i / 8] |= static_cast<uint8_t>(1u << (i % 8));
    tmp.frame[i] = static_cast<uint8_t>(val);
  }

  sanitiseLabel(obj["label"].as<const char*>(), tmp.label);
  sanitiseName(obj["name"].as<const char*>(), tmp.name);
  tmp.present = true;
  return static_cast<uint8_t>(button);
}

}  // namespace

LoadReport parseScenes(const char* json, size_t len, SceneSet& out) {
  out.clear();
  LoadReport rep;

  JsonDocument doc;
  if (deserializeJson(doc, json, len)) {
    rep.status = LoadStatus::ParseError;
    return rep;
  }
  JsonVariantConst version = doc["version"];
  if (!version.is<int>() || version.as<int>() != kSceneFormatVersion) {
    rep.status = LoadStatus::BadFormat;
    return rep;
  }
  if (!doc["scenes"].is<JsonArrayConst>()) {
    rep.status = LoadStatus::BadFormat;
    return rep;
  }
  JsonArrayConst scenes = doc["scenes"].as<JsonArrayConst>();

  for (JsonVariantConst v : scenes) {
    Scene tmp;
    const uint8_t button = v.is<JsonObjectConst>() ? loadOne(v.as<JsonObjectConst>(), tmp) : 0;
    if (button == 0 || out.scenes[button].present) {  // invalid, or duplicate button (first wins)
      if (rep.rejected < 255) rep.rejected++;
      continue;
    }
    out.scenes[button] = tmp;
    out.loaded++;
  }

  // Fixtures are display only: bad entries are skipped and never affect scenes.
  if (doc["fixtures"].is<JsonArrayConst>()) {
    for (JsonVariantConst v : doc["fixtures"].as<JsonArrayConst>()) {
      if (out.fixtureCount >= kMaxFixtures) break;
      if (!v["name"].is<const char*>() || !v["address"].is<int>() || !v["channels"].is<int>()) continue;
      const int addr = v["address"].as<int>();
      const int chans = v["channels"].as<int>();
      if (addr < 1 || chans < 1 || addr + chans - 1 > kDmxSlots) continue;
      FixtureInfo& f = out.fixtures[out.fixtureCount++];
      sanitiseName(v["name"].as<const char*>(), f.name);
      f.address = static_cast<uint16_t>(addr);
      f.channels = static_cast<uint16_t>(chans);
    }
  }

  rep.loaded = out.loaded;
  rep.status = out.loaded ? LoadStatus::Ok : LoadStatus::Empty;
  return rep;
}

uint8_t ButtonDebouncer::update(uint8_t raw) {
  if (popcount8(raw) > kMaxSimultaneousPresses) {
    if (faultStreak_ < 255) faultStreak_++;
    count_ = 0;
    return 0;
  }
  faultStreak_ = 0;

  if (raw == candidate_) {
    if (count_ < 255) count_++;
  } else {
    candidate_ = raw;
    count_ = 1;
  }

  if (count_ >= need_ && candidate_ != stable_) {
    const uint8_t pressed = static_cast<uint8_t>(candidate_ & ~stable_);
    stable_ = candidate_;
    return pressed;
  }
  return 0;
}

uint8_t pickAction(uint8_t pressedMask, uint8_t heldMask) {
  if (!pressedMask) return 0;
  if ((pressedMask | heldMask) & (1u << (kBlankButton - 1))) return kBlankButton;
  for (uint8_t i = 0; i < kNumButtons; ++i) {
    if (pressedMask & (1u << i)) return static_cast<uint8_t>(i + 1);
  }
  return 0;
}

void formatSceneText(char out[9], uint8_t button, const char* label) {
  snprintf(out, 9, "%u %-6.6s", static_cast<unsigned>(button % 10), label ? label : "");
}

void formatStatus(char out[9], const char* text) {
  snprintf(out, 9, "%-8.8s", text ? text : "");
}

int16_t marqueeOffset(uint16_t textPx, uint16_t windowPx, uint32_t tick, uint8_t pauseTicks) {
  if (textPx <= windowPx) return 0;
  const uint32_t overflow = textPx - windowPx;
  const uint32_t pos = tick % (overflow + 2u * pauseTicks);
  if (pos < pauseTicks) return 0;
  const uint32_t scrolled = pos - pauseTicks;
  return -static_cast<int16_t>(scrolled < overflow ? scrolled : overflow);
}

uint8_t barHeight(uint8_t value, uint8_t maxPx) {
  if (value == 0) return 0;
  const uint8_t h = static_cast<uint8_t>((static_cast<uint16_t>(value) * maxPx) / 255u);
  return h ? h : 1;
}

}  // namespace scenelogic

// Host tests for lib/scenelogic.  Run: pio test -e native
#include <scenelogic.h>
#include <string.h>
#include <unity.h>

using namespace scenelogic;

static SceneSet set;  // big: keep off the stack

static LoadReport parse(const char* json) { return parseScenes(json, strlen(json), set); }

void setUp() {}
void tearDown() {}

// Same shape tools/qxw_to_scenes.py writes for tools/tests/sample.qxw
static const char* kConverterOutput = R"({
  "version": 1,
  "source": "sample.qxw",
  "scenes": [
    {
      "button": 1,
      "label": "Tunnel",
      "name": "1 Tunnel",
      "values": [[1,64],[2,200],[3,150],[8,40]]
    },
    {
      "button": 2,
      "label": "Fan sw",
      "name": "2-Fan sweep",
      "values": [[1,100],[2,180],[4,140],[20,255]]
    }
  ]
})";

void test_parses_converter_output() {
  LoadReport r = parse(kConverterOutput);
  TEST_ASSERT_EQUAL_INT((int)LoadStatus::Ok, (int)r.status);
  TEST_ASSERT_EQUAL_UINT8(2, r.loaded);
  TEST_ASSERT_EQUAL_UINT8(0, r.rejected);
  TEST_ASSERT_TRUE(set.scenes[1].present);
  TEST_ASSERT_EQUAL_STRING("Tunnel", set.scenes[1].label);
  TEST_ASSERT_EQUAL_UINT8(64, set.scenes[1].frame[0]);
  TEST_ASSERT_EQUAL_UINT8(0, set.scenes[1].frame[3]);   // unset -> 0
  TEST_ASSERT_EQUAL_UINT8(40, set.scenes[1].frame[7]);
  TEST_ASSERT_EQUAL_UINT8(255, set.scenes[2].frame[19]);
  TEST_ASSERT_FALSE(set.scenes[3].present);
}

void test_bad_json_loads_nothing() {
  parse(kConverterOutput);  // populate first, to prove it gets cleared
  LoadReport r = parse("{\"version\":1,\"scenes\":[");
  TEST_ASSERT_EQUAL_INT((int)LoadStatus::ParseError, (int)r.status);
  TEST_ASSERT_EQUAL_UINT8(0, set.loaded);
  TEST_ASSERT_FALSE(set.scenes[1].present);
}

void test_wrong_version_rejected() {
  TEST_ASSERT_EQUAL_INT((int)LoadStatus::BadFormat, (int)parse("{\"version\":2,\"scenes\":[]}").status);
  TEST_ASSERT_EQUAL_INT((int)LoadStatus::BadFormat, (int)parse("{\"scenes\":[]}").status);
  TEST_ASSERT_EQUAL_INT((int)LoadStatus::BadFormat, (int)parse("{\"version\":1}").status);
}

void test_empty_is_valid() {
  LoadReport r = parse("{\"version\":1,\"scenes\":[]}");
  TEST_ASSERT_EQUAL_INT((int)LoadStatus::Empty, (int)r.status);
  TEST_ASSERT_EQUAL_UINT8(0, r.rejected);
}

void test_invalid_scenes_rejected_whole() {
  LoadReport r = parse(R"({"version":1,"scenes":[
    {"button":8,"values":[[1,10]]},
    {"button":0,"values":[[1,10]]},
    {"button":1,"values":[[1,10],[513,5]]},
    {"button":2,"values":[[1,256]]},
    {"button":3,"values":[[1,5],[1,6]]},
    {"button":4,"values":[[1]]},
    {"button":5,"values":"nope"},
    {"button":"6","values":[]},
    42,
    {"button":7,"label":"ok","values":[[512,1]]}
  ]})");
  TEST_ASSERT_EQUAL_INT((int)LoadStatus::Ok, (int)r.status);
  TEST_ASSERT_EQUAL_UINT8(1, r.loaded);
  TEST_ASSERT_EQUAL_UINT8(9, r.rejected);
  TEST_ASSERT_FALSE(set.scenes[1].present);  // not partially applied
  TEST_ASSERT_EQUAL_UINT8(0, set.scenes[1].frame[0]);
  TEST_ASSERT_TRUE(set.scenes[7].present);
  TEST_ASSERT_EQUAL_UINT8(1, set.scenes[7].frame[511]);
}

void test_duplicate_button_first_wins() {
  LoadReport r = parse(R"({"version":1,"scenes":[
    {"button":1,"label":"first","values":[[1,10]]},
    {"button":1,"label":"second","values":[[1,20]]}
  ]})");
  TEST_ASSERT_EQUAL_UINT8(1, r.loaded);
  TEST_ASSERT_EQUAL_UINT8(1, r.rejected);
  TEST_ASSERT_EQUAL_STRING("first", set.scenes[1].label);
}

void test_label_sanitised() {
  char out[kLabelMax + 1];
  sanitiseLabel("Fan.sweep long", out);
  TEST_ASSERT_EQUAL_STRING("Fanswe", out);
  sanitiseLabel("a\tb\xC3\xA9", out);
  TEST_ASSERT_EQUAL_STRING("ab", out);
  sanitiseLabel(nullptr, out);
  TEST_ASSERT_EQUAL_STRING("", out);
}

void test_debounce_needs_stable_reads() {
  ButtonDebouncer d(3);
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x01));
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x01));
  TEST_ASSERT_EQUAL_HEX8(0x01, d.update(0x01));  // third stable read
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x01));     // held: no repeat
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x00));
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x00));
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x00));     // release is not a press
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x01));     // bounce...
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x00));
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x01));
  TEST_ASSERT_EQUAL_HEX8(0, d.update(0x01));
  TEST_ASSERT_EQUAL_HEX8(0x01, d.update(0x01));
}

void test_debounce_flags_disconnected_panel() {
  ButtonDebouncer d(3);
  for (int i = 0; i < 10; ++i) TEST_ASSERT_EQUAL_HEX8(0, d.update(0xFF));
  TEST_ASSERT_EQUAL_UINT8(10, d.faultStreak());
  TEST_ASSERT_EQUAL_HEX8(0, d.held());
  d.update(0x00);
  TEST_ASSERT_EQUAL_UINT8(0, d.faultStreak());
}

void test_pick_action_blank_wins() {
  TEST_ASSERT_EQUAL_UINT8(8, pickAction(0x81));
  TEST_ASSERT_EQUAL_UINT8(1, pickAction(0x01));
  TEST_ASSERT_EQUAL_UINT8(3, pickAction(0x0C));
  TEST_ASSERT_EQUAL_UINT8(0, pickAction(0x00));
}

void test_display_text_is_eight_chars() {
  char out[9];
  formatSceneText(out, 1, "Tunnel");
  TEST_ASSERT_EQUAL_STRING("1 Tunnel", out);
  formatSceneText(out, 3, "ab");
  TEST_ASSERT_EQUAL_STRING("3 ab    ", out);
  formatStatus(out, "bLAnK");
  TEST_ASSERT_EQUAL_STRING("bLAnK   ", out);
  formatStatus(out, "much too long");
  TEST_ASSERT_EQUAL_STRING("much too", out);
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_parses_converter_output);
  RUN_TEST(test_bad_json_loads_nothing);
  RUN_TEST(test_wrong_version_rejected);
  RUN_TEST(test_empty_is_valid);
  RUN_TEST(test_invalid_scenes_rejected_whole);
  RUN_TEST(test_duplicate_button_first_wins);
  RUN_TEST(test_label_sanitised);
  RUN_TEST(test_debounce_needs_stable_reads);
  RUN_TEST(test_debounce_flags_disconnected_panel);
  RUN_TEST(test_pick_action_blank_wins);
  RUN_TEST(test_display_text_is_eight_chars);
  return UNITY_END();
}

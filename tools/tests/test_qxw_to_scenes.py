"""Tests for tools/qxw_to_scenes.py.  Run: python3 -m pytest tools/tests -q"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import qxw_to_scenes as q  # noqa: E402

SAMPLE = HERE / "sample.qxw"


def workspace(tmp_path, functions: str, fixtures: str | None = None, ns: bool = True) -> Path:
    if fixtures is None:
        fixtures = """
  <Fixture><Model>Dual head Laser</Model><ID>0</ID><Name>Laser</Name>
   <Universe>0</Universe><Address>0</Address><Channels>10</Channels></Fixture>"""
    xmlns = ' xmlns="http://www.qlcplus.org/Workspace"' if ns else ""
    p = tmp_path / "ws.qxw"
    p.write_text(f'<?xml version="1.0"?><Workspace{xmlns}><Engine>{fixtures}{functions}</Engine></Workspace>')
    return p


def scene(name: str, *fixture_vals: tuple[int, str]) -> str:
    inner = "".join(f'<FixtureVal ID="{fid}">{vals}</FixtureVal>' for fid, vals in fixture_vals)
    return f'<Function Type="Scene" Name="{name}">{inner}</Function>'


def test_sample_converts():
    res = q.convert_workspace(SAMPLE)
    assert res.errors == []
    assert [s["button"] for s in res.scenes] == [1, 2]
    one, two = res.scenes
    assert one["label"] == "Tunnel"
    # channel indices are 0-based in .qxw, addresses 1-based out; zeros dropped
    assert one["values"] == [[1, 64], [2, 200], [3, 150], [8, 40]]
    # second fixture at 0-based address 19 -> DMX 20
    assert two["values"] == [[1, 100], [2, 180], [4, 140], [20, 255]]
    assert two["label"] == "Fan sw"
    assert any("no leading button number" in w for w in res.warnings)
    assert not any("Not a scene" in s["name"] for s in res.scenes)  # chasers ignored


def test_cli_writes_valid_file(tmp_path, capsys):
    out = tmp_path / "scenes.json"
    assert q.main([str(SAMPLE), "-o", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["version"] == q.FORMAT_VERSION
    assert q.validate_document(doc) == []
    assert q.main(["--check", str(out)]) == 0


def test_blank_button_rejected(tmp_path):
    res = q.convert_workspace(workspace(tmp_path, scene("8 Oops", (0, "0,64"))))
    assert any("hard-wired BLANK" in e for e in res.errors)


def test_out_of_range_button_rejected(tmp_path):
    res = q.convert_workspace(workspace(tmp_path, scene("9 Nope", (0, "0,64"))))
    assert res.errors


def test_duplicate_button_rejected(tmp_path):
    fns = scene("1 A", (0, "0,64")) + scene("1 B", (0, "0,70"))
    res = q.convert_workspace(workspace(tmp_path, fns))
    assert any("used by both" in e for e in res.errors)


def test_conflicting_overlap_rejected(tmp_path):
    fixtures = """
  <Fixture><ID>0</ID><Name>A</Name><Universe>0</Universe><Address>0</Address><Channels>4</Channels></Fixture>
  <Fixture><ID>1</ID><Name>B</Name><Universe>0</Universe><Address>2</Address><Channels>4</Channels></Fixture>"""
    res = q.convert_workspace(workspace(tmp_path, scene("1 X", (0, "2,10"), (1, "0,20")), fixtures))
    assert any("address 3" in e for e in res.errors)
    assert any("overlap" in w for w in res.warnings)


def test_other_universe_rejected(tmp_path):
    fixtures = """
  <Fixture><ID>0</ID><Name>Far</Name><Universe>1</Universe><Address>0</Address><Channels>4</Channels></Fixture>"""
    res = q.convert_workspace(workspace(tmp_path, scene("1 X", (0, "0,10")), fixtures))
    assert any("universe 2" in e for e in res.errors)


def test_channel_out_of_range_rejected(tmp_path):
    res = q.convert_workspace(workspace(tmp_path, scene("1 X", (0, "10,5"))))
    assert any("out of range" in e for e in res.errors)


def test_no_namespace_still_parses(tmp_path):
    res = q.convert_workspace(workspace(tmp_path, scene("3 Spin", (0, "0,64")), ns=False))
    assert res.errors == [] and res.scenes[0]["button"] == 3


def test_label_sanitised():
    assert q.sanitise_label("Fan.sweep")[0] == "Fanswe"
    assert q.sanitise_label("Tunnel")[0] == "Tunnel"
    assert q.sanitise_label("Café")[0] == "Caf"


def test_all_zero_scene_warns(tmp_path):
    res = q.convert_workspace(workspace(tmp_path, scene("4 Dark", (0, "0,0"))))
    assert res.errors == [] and res.scenes[0]["values"] == []
    assert any("same as BLANK" in w for w in res.warnings)


def test_errors_mean_nothing_written(tmp_path):
    out = tmp_path / "scenes.json"
    ws = workspace(tmp_path, scene("8 Oops", (0, "0,64")))
    assert q.main([str(ws), "-o", str(out)]) == 1
    assert not out.exists()


def test_validate_document_catches_firmware_rejections():
    bad = {
        "version": 1,
        "scenes": [
            {"button": 8, "values": []},
            {"button": 1, "values": [[0, 5]]},
            {"button": 2, "values": [[1, 256]]},
            {"button": 3, "values": [[1, 5], [1, 6]]},
            {"button": True, "values": []},
        ],
    }
    assert len(q.validate_document(bad)) == 5
    assert q.validate_document({"version": 2, "scenes": []})


def test_fixtures_listed_for_display():
    res = q.convert_workspace(SAMPLE)
    # only fixtures a scene actually uses, by address, 1-based
    assert [f["address"] for f in res.fixtures] == sorted(f["address"] for f in res.fixtures)
    assert all(1 <= f["address"] <= 512 and f["channels"] >= 1 for f in res.fixtures)
    assert "Dual head Laser" in [f["name"] for f in res.fixtures]
    doc = json.loads(q.render_json("sample.qxw", res.scenes, res.fixtures))
    assert doc["fixtures"] == res.fixtures
    assert q.validate_document(doc) == []


def test_unused_fixture_not_listed(tmp_path):
    fixtures = """
  <Fixture><ID>0</ID><Name>Used</Name><Universe>0</Universe><Address>0</Address><Channels>4</Channels></Fixture>
  <Fixture><ID>1</ID><Name>Idle</Name><Universe>0</Universe><Address>10</Address><Channels>4</Channels></Fixture>"""
    res = q.convert_workspace(workspace(tmp_path, scene("1 X", (0, "0,64")), fixtures))
    assert res.fixtures == [{"name": "Used", "address": 1, "channels": 4}]


def test_bad_fixtures_reported():
    doc = {"version": 1, "scenes": [], "fixtures": [{"name": "A", "address": 0, "channels": 4}, "x"]}
    assert len(q.validate_document(doc)) == 2
    assert q.validate_document({"version": 1, "scenes": []}) == []  # fixtures optional

#!/usr/bin/env python3
"""Convert QLC+ workspace scenes into data/scenes.json for minidesk.

Name each QLC+ scene with its button number first: "1 Tunnel", "3-Fan sweep",
"7: Slow spin". Buttons 1-7 only; button 8 is the hard-wired BLANK.
Scenes without a leading number are skipped with a warning.

Or leave the names alone and pick scenes for buttons with --scene (by name, or
#ID). Giving one button several scenes merges them, e.g. a laser look plus a
wash from a full show file. Name numbers are then ignored.

    python3 tools/qxw_to_scenes.py my-show.qxw -o data/scenes.json
    python3 tools/qxw_to_scenes.py party.qxw --scene "1=Laser-hzLines" --scene "1=Pin-RB" --label 1=HzPin
    python3 tools/qxw_to_scenes.py --check data/scenes.json

Nothing is written if there are any errors. Exit code 1 on error.

Format v1 (must match lib/scenelogic):
    {"version": 1, "source": "...", "scenes": [
        {"button": 1, "label": "Tunnel", "name": "1 Tunnel", "values": [[1, 64], [2, 200]]}
    ], "fixtures": [{"name": "Dual head Laser", "address": 1, "channels": 10}]}
values are [DMX address 1-512, value 0-255]; channels not listed are 0, so zeros
are dropped from the output. "fixtures" (optional, display only) lists the patched
fixtures the scenes use, for the box's OLED.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

FORMAT_VERSION = 1
MAX_SCENE_BUTTON = 7
BLANK_BUTTON = 8
LABEL_MAX = 6
DMX_SLOTS = 512
NAME_RE = re.compile(r"^\s*(\d+)\s*[-_:.]?\s*(.*?)\s*$")


class ConvertError(Exception):
    pass


@dataclass
class Fixture:
    id: int
    name: str
    universe: int  # 0-based, as stored in .qxw
    address: int  # 0-based, as stored in .qxw
    channels: int


@dataclass
class SceneFixture:
    name: str
    universe: int  # 1-based
    address: int  # 1-based
    channels: int

    def dmx_range(self) -> str:
        last = self.address + self.channels - 1
        span = f"{self.address}-{last}" if last > self.address else str(self.address)
        return span if self.universe == 1 else f"U{self.universe} {span}"


@dataclass
class SceneInfo:
    id: int
    name: str
    button: int | None  # from a leading number in the name (1-7), if any
    fixtures: list[SceneFixture] = field(default_factory=list)  # the fixtures it sets, in scene order


@dataclass
class Assignment:
    """Scenes picked for one button; more than one are merged into a single look."""

    button: int
    scene_ids: list[int]
    label: str | None = None  # None: taken from the first scene's name


@dataclass
class Result:
    scenes: list[dict] = field(default_factory=list)
    fixtures: list[dict] = field(default_factory=list)  # display only
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem if _local(c.tag) == name]


def _child_text(elem: ET.Element, name: str, default: str = "") -> str:
    for c in elem:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return default


def sanitise_label(text: str) -> tuple[str, bool]:
    """Printable ASCII, no '.', max LABEL_MAX. Returns (label, was_changed)."""
    kept = "".join(ch for ch in text if 0x20 <= ord(ch) <= 0x7E and ch != ".")
    label = kept[:LABEL_MAX]
    return label, label != text


def default_label(name: str) -> str:
    """A display label from a scene name: drop a leading button number, and for
    "Fixture-look" names keep the look ("Laser-hzLines" -> "hzLine")."""
    m = NAME_RE.match(name)
    text = m.group(2) if m and m.group(2) else name
    if "-" in text.strip("- "):
        text = text.strip("- ").split("-", 1)[1]
    return sanitise_label(text.strip())[0]


def parse_fixtures(engine: ET.Element, res: Result) -> dict[int, Fixture]:
    fixtures: dict[int, Fixture] = {}
    for fx in _children(engine, "Fixture"):
        try:
            f = Fixture(
                id=int(_child_text(fx, "ID")),
                name=_child_text(fx, "Name") or _child_text(fx, "Model"),
                universe=int(_child_text(fx, "Universe", "0")),
                address=int(_child_text(fx, "Address")),
                channels=int(_child_text(fx, "Channels")),
            )
        except ValueError:
            res.errors.append(f"fixture with unreadable ID/Address/Channels: {ET.tostring(fx)[:80]!r}")
            continue
        fixtures[f.id] = f

    # Patch overlap is legal in QLC+ but almost always a mistake here.
    used: dict[int, str] = {}
    for f in fixtures.values():
        for ch in range(f.channels):
            key = f.universe * DMX_SLOTS + f.address + ch
            if key in used and used[key] != f.name:
                res.warnings.append(
                    f"fixtures '{used[key]}' and '{f.name}' overlap at universe {f.universe + 1} "
                    f"address {f.address + ch + 1}"
                )
                break
            used[key] = f.name
    return fixtures


def _collect_values(
    func: ET.Element,
    fixtures: dict[int, Fixture],
    res: Result,
    values: dict[int, int],
    owner: dict[int, str],
    merged: bool = False,
) -> bool:
    """Add one scene's channel values to `values` (1-based address -> value).
    Errors go to res; returns False if the scene had any. `merged` names the
    scene in conflict messages, for buttons built from several scenes."""
    name = func.get("Name", "")
    ok = True
    for fv in _children(func, "FixtureVal"):
        fid_s = fv.get("ID", "")
        fx = fixtures.get(int(fid_s)) if fid_s.strip().lstrip("-").isdigit() else None
        if fx is None:
            res.errors.append(f"scene '{name}': references unknown fixture ID {fid_s}")
            ok = False
            continue
        text = (fv.text or "").strip()
        if not text:
            continue
        parts = [p.strip() for p in text.split(",")]
        if len(parts) % 2:
            res.errors.append(f"scene '{name}': odd-length value list for fixture '{fx.name}'")
            ok = False
            continue
        if fx.universe != 0:
            res.errors.append(
                f"scene '{name}': fixture '{fx.name}' is on universe {fx.universe + 1}; only universe 1 is sent"
            )
            ok = False
            continue
        for ch_s, val_s in zip(parts[0::2], parts[1::2]):
            try:
                ch, val = int(ch_s), int(val_s)
            except ValueError:
                res.errors.append(f"scene '{name}': non-numeric value pair '{ch_s},{val_s}' for '{fx.name}'")
                ok = False
                continue
            if not 0 <= ch < fx.channels:
                res.errors.append(f"scene '{name}': channel {ch + 1} out of range for '{fx.name}'")
                ok = False
                continue
            if not 0 <= val <= 255:
                res.errors.append(f"scene '{name}': value {val} out of range")
                ok = False
                continue
            addr = fx.address + ch + 1
            if addr > DMX_SLOTS:
                res.errors.append(f"scene '{name}': '{fx.name}' channel {ch + 1} lands past address 512")
                ok = False
                continue
            who = f"'{fx.name}' in '{name}'" if merged else f"'{fx.name}'"
            if addr in values and values[addr] != val:
                res.errors.append(f"scene '{name}': address {addr} set to {values[addr]} by {owner[addr]} and {val} by {who}")
                ok = False
                continue
            values[addr] = val
            owner[addr] = who
    return ok


def _used_fixture_ids(func: ET.Element, fixtures: dict[int, Fixture]) -> set[int]:
    return {fixtures[int(fv.get("ID"))].id for fv in _children(func, "FixtureVal") if (fv.text or "").strip()}


def _scene_dict(button: int, label: str, name: str, values: dict[int, int]) -> dict:
    return {
        "button": button,
        "label": label,
        "name": name,
        "values": [[a, v] for a, v in sorted(values.items()) if v != 0],
    }


def convert_scene(
    func: ET.Element, fixtures: dict[int, Fixture], res: Result, used: set[int] | None = None
) -> dict | None:
    name = func.get("Name", "")
    m = NAME_RE.match(name)
    if not m:
        res.warnings.append(f"scene '{name}' has no leading button number, skipped")
        return None
    button = int(m.group(1))
    if button == BLANK_BUTTON:
        res.errors.append(f"scene '{name}': button {BLANK_BUTTON} is the hard-wired BLANK")
        return None
    if not 1 <= button <= MAX_SCENE_BUTTON:
        res.errors.append(f"scene '{name}': button must be 1-{MAX_SCENE_BUTTON}")
        return None

    values: dict[int, int] = {}  # 1-based DMX address -> value
    owner: dict[int, str] = {}
    ok = _collect_values(func, fixtures, res, values, owner)
    if not ok:
        return None
    if used is not None:
        used.update(_used_fixture_ids(func, fixtures))

    label, changed = sanitise_label(m.group(2))
    if changed:
        res.warnings.append(f"scene '{name}': display label shortened to '{label}'")
    if not any(values.values()):
        res.warnings.append(f"scene '{name}': every channel is 0, which is the same as BLANK")

    return _scene_dict(button, label, name, values)


def convert_assigned(
    engine: ET.Element, fixtures: dict[int, Fixture], assignments: list[Assignment], res: Result, used: set[int]
) -> None:
    """Build buttons from picked scenes, merging several into one where asked.
    A button with any error is dropped whole, and the error stops the upload."""
    by_id = {int(f.get("ID")): f for f in _children(engine, "Function") if _scene_id(f) is not None}
    seen: set[int] = set()
    for a in sorted(assignments, key=lambda a: a.button):
        if not a.scene_ids:
            continue
        if a.button == BLANK_BUTTON:
            res.errors.append(f"button {BLANK_BUTTON} is the hard-wired BLANK; it can't have a scene")
            continue
        if not 1 <= a.button <= MAX_SCENE_BUTTON:
            res.errors.append(f"button {a.button}: buttons are 1-{MAX_SCENE_BUTTON}")
            continue
        if a.button in seen:
            res.errors.append(f"button {a.button} assigned twice")
            continue
        seen.add(a.button)

        funcs = []
        for sid in dict.fromkeys(a.scene_ids):  # keep order, drop repeats
            if sid in by_id:
                funcs.append(by_id[sid])
            else:
                res.errors.append(f"button {a.button}: the workspace has no scene with ID {sid}")
        if len(funcs) != len(dict.fromkeys(a.scene_ids)):
            continue
        values: dict[int, int] = {}
        owner: dict[int, str] = {}
        ok = True
        for f in funcs:
            ok = _collect_values(f, fixtures, res, values, owner, merged=len(funcs) > 1) and ok
        if not ok:
            continue
        for f in funcs:
            used.update(_used_fixture_ids(f, fixtures))

        names = [f.get("Name", "") for f in funcs]
        label, changed = sanitise_label(a.label if a.label is not None else default_label(names[0]))
        if changed and a.label is not None:
            res.warnings.append(f"button {a.button}: display label shortened to '{label}'")
        if not any(values.values()):
            res.warnings.append(f"button {a.button}: every channel is 0, which is the same as BLANK")
        res.scenes.append(_scene_dict(a.button, label, " + ".join(names), values))


def _scene_id(func: ET.Element) -> int | None:
    if func.get("Type") != "Scene":
        return None
    sid = func.get("ID", "").strip()
    return int(sid) if sid.isdigit() else None


def _load_engine(path: Path, res: Result) -> ET.Element | None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        res.errors.append(f"{path}: not valid XML ({e})")
        return None
    if _local(root.tag) != "Workspace":
        res.errors.append(f"{path}: root element is <{_local(root.tag)}>, expected <Workspace> (.qxw file)")
        return None
    engines = _children(root, "Engine")
    if not engines:
        res.errors.append(f"{path}: no <Engine> element")
        return None
    return engines[0]


def list_scenes(path: Path) -> tuple[list[SceneInfo], list[str]]:
    """Every scene a user can pick, in workspace order, plus any load errors.
    Hidden scenes (the steps QLC+ keeps inside sequences) are left out."""
    res = Result()
    engine = _load_engine(path, res)
    if engine is None:
        return [], res.errors
    fixtures = parse_fixtures(engine, Result())  # problems are reported when converting
    scenes = []
    for f in _children(engine, "Function"):
        sid = _scene_id(f)
        if sid is None or f.get("Hidden") == "True":
            continue
        name = f.get("Name", "")
        m = NAME_RE.match(name)
        button = int(m.group(1)) if m and 1 <= int(m.group(1)) <= MAX_SCENE_BUTTON else None
        info = SceneInfo(sid, name, button)
        for fv in _children(f, "FixtureVal"):
            fid = fv.get("ID", "").strip()
            fx = fixtures.get(int(fid)) if fid.isdigit() else None
            if fx is not None and (fv.text or "").strip():
                info.fixtures.append(SceneFixture(fx.name, fx.universe + 1, fx.address + 1, fx.channels))
        scenes.append(info)
    return scenes, []


def resolve_picks(scenes: list[SceneInfo], picks: list[str], labels: list[str]) -> tuple[list[Assignment], list[str]]:
    """Turn CLI picks ("1=Scene name" or "1=#40") and labels ("1=Text") into assignments."""
    errors: list[str] = []
    by_button: dict[int, Assignment] = {}

    def split(item: str, what: str) -> tuple[int, str] | None:
        b, sep, rest = item.partition("=")
        if not sep or not b.strip().isdigit():
            errors.append(f"{what} '{item}': expected BUTTON=...")
            return None
        return int(b), rest.strip()

    for item in picks:
        got = split(item, "--scene")
        if got is None:
            continue
        button, ref = got
        if ref.startswith("#") and ref[1:].isdigit():
            matches = [s for s in scenes if s.id == int(ref[1:])]
        else:
            matches = [s for s in scenes if s.name == ref]
        if len(matches) != 1:
            errors.append(f"--scene '{item}': {'no' if not matches else 'more than one'} scene called '{ref}'"
                          + (" (use #ID)" if matches else ""))
            continue
        by_button.setdefault(button, Assignment(button, [])).scene_ids.append(matches[0].id)
    for item in labels:
        got = split(item, "--label")
        if got is None:
            continue
        button, text = got
        if button not in by_button:
            errors.append(f"--label '{item}': button {button} has no --scene")
            continue
        by_button[button].label = text
    return list(by_button.values()), errors


def convert_workspace(path: Path, assignments: list[Assignment] | None = None) -> Result:
    """Scenes for the box. Without assignments, buttons come from leading numbers
    in scene names; with them, only the assigned scenes are used."""
    res = Result()
    engine = _load_engine(path, res)
    if engine is None:
        return res

    fixtures = parse_fixtures(engine, res)
    used: set[int] = set()
    if assignments is not None:
        convert_assigned(engine, fixtures, assignments, res, used)
    else:
        seen: dict[int, str] = {}
        for func in _children(engine, "Function"):
            if func.get("Type") != "Scene":
                continue
            scene = convert_scene(func, fixtures, res, used)
            if scene is None:
                continue
            b = scene["button"]
            if b in seen:
                res.errors.append(f"button {b} used by both '{seen[b]}' and '{scene['name']}'")
                continue
            seen[b] = scene["name"]
            res.scenes.append(scene)

    res.scenes.sort(key=lambda s: s["button"])
    res.fixtures = [
        {"name": f.name, "address": f.address + 1, "channels": f.channels}
        for f in sorted((fixtures[i] for i in used), key=lambda f: f.address)
    ]
    if not res.scenes and not res.errors:
        what = "scenes assigned" if assignments is not None else "numbered scenes found"
        res.warnings.append(f"no {what}; the box will only ever be BLANK")
    return res


def validate_document(doc: object) -> list[str]:
    """Mirror of the firmware's parseScenes() rules. Returns a list of problems."""
    problems: list[str] = []
    if not isinstance(doc, dict) or doc.get("version") != FORMAT_VERSION:
        return [f"version must be {FORMAT_VERSION}"]
    scenes = doc.get("scenes")
    if not isinstance(scenes, list):
        return ["'scenes' must be a list"]
    seen: set[int] = set()
    for i, s in enumerate(scenes):
        where = f"scenes[{i}]"
        if not isinstance(s, dict):
            problems.append(f"{where}: not an object")
            continue
        b = s.get("button")
        if not isinstance(b, int) or isinstance(b, bool) or not 1 <= b <= MAX_SCENE_BUTTON:
            problems.append(f"{where}: button must be an integer 1-{MAX_SCENE_BUTTON}")
        elif b in seen:
            problems.append(f"{where}: duplicate button {b} (firmware keeps the first)")
        else:
            seen.add(b)
        vals = s.get("values")
        if not isinstance(vals, list):
            problems.append(f"{where}: 'values' must be a list")
            continue
        addrs: set[int] = set()
        for pair in vals:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in pair)
            ):
                problems.append(f"{where}: bad pair {pair!r}")
                break
            a, v = pair
            if not 1 <= a <= DMX_SLOTS or not 0 <= v <= 255:
                problems.append(f"{where}: out of range pair {pair!r}")
                break
            if a in addrs:
                problems.append(f"{where}: duplicate address {a}")
                break
            addrs.add(a)

    fixtures = doc.get("fixtures", [])
    if not isinstance(fixtures, list):
        problems.append("'fixtures' must be a list")
        fixtures = []
    for i, f in enumerate(fixtures):
        # Display only: the firmware skips bad entries rather than rejecting scenes.
        ok = (
            isinstance(f, dict)
            and isinstance(f.get("name"), str)
            and all(isinstance(f.get(k), int) and not isinstance(f.get(k), bool) for k in ("address", "channels"))
        )
        if not ok or not 1 <= f["address"] <= DMX_SLOTS or not 1 <= f["channels"] <= DMX_SLOTS:
            problems.append(f"fixtures[{i}]: needs name, address 1-{DMX_SLOTS}, channels 1-{DMX_SLOTS}")
    return problems


def render_json(source: str, scenes: list[dict], fixtures: list[dict] | None = None) -> str:
    """One scene per block, values on one line, so diffs stay readable."""
    lines = ["{", f'  "version": {FORMAT_VERSION},', f'  "source": {json.dumps(source)},', '  "scenes": [']
    for i, s in enumerate(scenes):
        sep = "," if i < len(scenes) - 1 else ""
        lines += [
            "    {",
            f'      "button": {s["button"]},',
            f'      "label": {json.dumps(s["label"])},',
            f'      "name": {json.dumps(s["name"])},',
            f'      "values": {json.dumps(s["values"], separators=(",", ":"))}',
            "    }" + sep,
        ]
    lines.append("  ]," if fixtures else "  ]")
    if fixtures:
        lines.append('  "fixtures": [')
        for i, f in enumerate(fixtures):
            sep = "," if i < len(fixtures) - 1 else ""
            lines.append(f"    {json.dumps(f)}{sep}")
        lines.append("  ]")
    lines += ["}", ""]
    return "\n".join(lines)


def print_summary(scenes: list[dict]) -> None:
    print(f"{'btn':<4}{'label':<8}{'chans':>6}  name")
    for s in scenes:
        print(f"{s['button']:<4}{s['label']:<8}{len(s['values']):>6}  {s['name']}")
    print(f"{BLANK_BUTTON:<4}{'bLAnK':<8}{0:>6}  (hard-wired)")


def add_pick_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--scene", action="append", default=[], metavar="BUTTON=NAME",
                    help="put this scene (name, or #ID) on a button; repeat a button to merge scenes. "
                    "Name numbers are then ignored")
    ap.add_argument("--label", action="append", default=[], metavar="BUTTON=TEXT", help="display label for a button")


def picks_from_args(args: argparse.Namespace) -> tuple[list[Assignment] | None, list[str]]:
    if not args.scene:
        return None, (["--label needs --scene"] if args.label else [])
    scenes, errors = list_scenes(args.workspace)
    if errors:
        return None, []  # convert_workspace reports the same load errors
    return resolve_picks(scenes, args.scene, args.label)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace", nargs="?", type=Path, help="QLC+ workspace (.qxw)")
    ap.add_argument("-o", "--output", type=Path, default=Path("data/scenes.json"))
    ap.add_argument("--check", type=Path, metavar="JSON", help="validate an existing scenes.json and exit")
    add_pick_args(ap)
    args = ap.parse_args(argv)

    if args.check:
        try:
            doc = json.loads(args.check.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        problems = validate_document(doc)
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        if not problems:
            print_summary(doc["scenes"])
        return 1 if problems else 0

    if not args.workspace:
        ap.error("give a .qxw workspace, or --check a scenes.json")

    assignments, pick_errors = picks_from_args(args)
    res = convert_workspace(args.workspace, assignments)
    res.errors[:0] = pick_errors
    for w in res.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in res.errors:
        print(f"error: {e}", file=sys.stderr)
    if res.errors:
        print(f"{len(res.errors)} error(s); {args.output} not written", file=sys.stderr)
        return 1

    text = render_json(args.workspace.name, res.scenes, res.fixtures)
    problems = validate_document(json.loads(text))  # belt and braces
    if problems:
        for p in problems:
            print(f"internal error: {p}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(args.output)
    print_summary(res.scenes)
    print(f"wrote {args.output} - now run: pio run -t uploadfs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

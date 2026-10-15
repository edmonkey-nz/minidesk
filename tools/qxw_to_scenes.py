#!/usr/bin/env python3
"""Convert QLC+ workspace scenes into data/scenes.json for minidesk.

Name each QLC+ scene with its button number first: "1 Tunnel", "3-Fan sweep",
"7: Slow spin". Buttons 1-7 only; button 8 is the hard-wired BLANK.
Scenes without a leading number are skipped with a warning.

    python3 tools/qxw_to_scenes.py my-show.qxw -o data/scenes.json
    python3 tools/qxw_to_scenes.py --check data/scenes.json

Nothing is written if there are any errors. Exit code 1 on error.

Format v1 (must match lib/scenelogic):
    {"version": 1, "source": "...", "scenes": [
        {"button": 1, "label": "Tunnel", "name": "1 Tunnel", "values": [[1, 64], [2, 200]]}
    ]}
values are [DMX address 1-512, value 0-255]; channels not listed are 0, so zeros
are dropped from the output.
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
class Result:
    scenes: list[dict] = field(default_factory=list)
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


def convert_scene(func: ET.Element, fixtures: dict[int, Fixture], res: Result) -> dict | None:
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
    ok = True
    for fv in _children(func, "FixtureVal"):
        fid_s = fv.get("ID", "")
        fx = fixtures.get(int(fid_s)) if fid_s.strip().lstrip("-").isdigit() else None
        fid = fid_s
        if fx is None:
            res.errors.append(f"scene '{name}': references unknown fixture ID {fid}")
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
            if addr in values and values[addr] != val:
                res.errors.append(
                    f"scene '{name}': address {addr} set to {values[addr]} by '{owner[addr]}' "
                    f"and {val} by '{fx.name}'"
                )
                ok = False
                continue
            values[addr] = val
            owner[addr] = fx.name
    if not ok:
        return None

    label, changed = sanitise_label(m.group(2))
    if changed:
        res.warnings.append(f"scene '{name}': display label shortened to '{label}'")
    if not any(values.values()):
        res.warnings.append(f"scene '{name}': every channel is 0, which is the same as BLANK")

    return {
        "button": button,
        "label": label,
        "name": name,
        "values": [[a, v] for a, v in sorted(values.items()) if v != 0],
    }


def convert_workspace(path: Path) -> Result:
    res = Result()
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        res.errors.append(f"{path}: not valid XML ({e})")
        return res
    if _local(root.tag) != "Workspace":
        res.errors.append(f"{path}: root element is <{_local(root.tag)}>, expected <Workspace> (.qxw file)")
        return res
    engines = _children(root, "Engine")
    if not engines:
        res.errors.append(f"{path}: no <Engine> element")
        return res
    engine = engines[0]

    fixtures = parse_fixtures(engine, res)
    seen: dict[int, str] = {}
    for func in _children(engine, "Function"):
        if func.get("Type") != "Scene":
            continue
        scene = convert_scene(func, fixtures, res)
        if scene is None:
            continue
        b = scene["button"]
        if b in seen:
            res.errors.append(f"button {b} used by both '{seen[b]}' and '{scene['name']}'")
            continue
        seen[b] = scene["name"]
        res.scenes.append(scene)

    res.scenes.sort(key=lambda s: s["button"])
    if not res.scenes and not res.errors:
        res.warnings.append("no numbered scenes found; the box will only ever be BLANK")
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
    return problems


def render_json(source: str, scenes: list[dict]) -> str:
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
    lines += ["  ]", "}", ""]
    return "\n".join(lines)


def print_summary(scenes: list[dict]) -> None:
    print(f"{'btn':<4}{'label':<8}{'chans':>6}  name")
    for s in scenes:
        print(f"{s['button']:<4}{s['label']:<8}{len(s['values']):>6}  {s['name']}")
    print(f"{BLANK_BUTTON:<4}{'bLAnK':<8}{0:>6}  (hard-wired)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace", nargs="?", type=Path, help="QLC+ workspace (.qxw)")
    ap.add_argument("-o", "--output", type=Path, default=Path("data/scenes.json"))
    ap.add_argument("--check", type=Path, metavar="JSON", help="validate an existing scenes.json and exit")
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

    res = convert_workspace(args.workspace)
    for w in res.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in res.errors:
        print(f"error: {e}", file=sys.stderr)
    if res.errors:
        print(f"{len(res.errors)} error(s); {args.output} not written", file=sys.stderr)
        return 1

    text = render_json(args.workspace.name, res.scenes)
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

#!/usr/bin/env python3
"""minidesk uploader: pick a QLC+ workspace, check its scenes, send them to the box.

A small tkinter front end over minidesk_flash.py. Packaged per OS by
tools/build_uploader.py; also runs from source:

    pip install -r tools/requirements-uploader.txt
    python3 tools/minidesk_uploader.py
"""
from __future__ import annotations

import datetime
import json
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import minidesk_flash as mf
import qxw_to_scenes as conv

REPO_URL = "https://github.com/edmonkey-nz/minidesk"
ISSUES_URL = REPO_URL + "/issues"
QLC_URL = "https://www.qlcplus.org/"

HERE = Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))


def bundled_firmware() -> Path | None:
    for p in (BUNDLE / "firmware" / "firmware.factory.bin", HERE.parent / ".pio/build/c3/firmware.factory.bin"):
        if p.is_file():
            return p
    return None


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "minidesk" / "uploader.json"


def load_picks(qxw: Path) -> dict | None:
    """Saved picks for this workspace: {"1": {"label": str|None, "scenes": [[id, name], ...]}}."""
    try:
        doc = json.loads(config_path().read_text(encoding="utf-8"))
        picks = doc["workspaces"][str(qxw.resolve())]
        return picks if isinstance(picks, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_picks(qxw: Path, picks: dict) -> None:
    """Best effort: losing the picks only means picking again."""
    path = config_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc.get("workspaces"), dict):
            raise ValueError
    except (OSError, ValueError, AttributeError):
        doc = {"workspaces": {}}
    doc["workspaces"][str(qxw.resolve())] = picks
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def build_info() -> str:
    try:
        info = json.loads((BUNDLE / "build_info.json").read_text())
        built = datetime.date.fromisoformat(info["date"])
        return f"{info['version']} · {built.day} {built:%b %Y}"
    except (OSError, ValueError, KeyError):
        return "running from source"


INFO_TEXT = """\
minidesk recalls up to seven static DMX scenes from an 8-button panel, for any \
fixtures on universe 1. Button 8 is always BLANK: every channel at 0.

How to use
1. In QLC+, patch your fixtures on universe 1 and build your scenes. A full \
show file is fine.
2. Choose the .qxw file here. Select scenes and press 1–7 (or the number \
buttons) to put them on a button. Several scenes on one button are merged \
into one look, e.g. a laser scene plus a wash. Double-click a button to change \
what the box displays. Scenes named with a number first ("1 Tunnel") start \
on that button. Picks are remembered for each workspace.
3. Channels a button doesn't set are sent as 0, so include mode, shutter and \
master-dimmer channels.
4. Plug the box in over USB, and make sure your fixtures are safe if DMX stops.
5. Upload scenes. The box restarts in BLANK and shows how many scenes it loaded.

Install firmware is for a new box, or after a firmware update. It writes the scenes too.

Why DMX stopping matters
While the box is being written to, it sends no DMX. Some fixtures go dark, some \
hold their last look, and some start an auto or sound mode. Power lasers off unless \
you've tested what they do."""


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("minidesk uploader")
        self.minsize(1080, 780)
        if "clam" in ttk.Style().theme_names() and sys.platform.startswith("linux"):
            ttk.Style().theme_use("clam")

        self.qxw: Path | None = None
        self.scenes: list[conv.SceneInfo] = []
        self.picks: dict[int, list[int]] = {}  # button -> scene IDs, in pick order
        self.labels: dict[int, str] = {}  # button -> display label the user typed
        self.pick_notes: list[str] = []  # from restoring saved picks
        self.converted: mf.Converted | None = None
        self.firmware = bundled_firmware()
        self.ports: list[mf.Port] = []
        self.busy = False
        self.events: queue.Queue = queue.Queue()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        main = ttk.Frame(self, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        info = ttk.Frame(self, padding=12)
        info.grid(row=0, column=1, sticky="ns")
        ttk.Separator(self, orient="vertical").grid(row=0, column=1, sticky="nsw")

        self._build_main(main)
        self._build_info(info)
        self.refresh_ports()
        self._update_buttons()
        self.after(100, self._drain_events)

    # --- layout ---------------------------------------------------------------

    def _build_main(self, f: ttk.Frame) -> None:
        f.columnconfigure(0, weight=1)

        ttk.Label(f, text="1. QLC+ workspace", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, sticky="w")
        row = ttk.Frame(f)
        row.grid(row=1, column=0, sticky="ew", pady=(4, 6))
        row.columnconfigure(0, weight=1)
        self.path_var = tk.StringVar(value="No workspace chosen")
        ttk.Label(row, textvariable=self.path_var, relief="sunken", padding=(6, 3)).grid(row=0, column=0, sticky="ew")
        ttk.Button(row, text="Choose .qxw…", command=self.choose_file).grid(row=0, column=1, padx=(6, 0))
        self.reload_btn = ttk.Button(row, text="Reload", command=self.reload_file)
        self.reload_btn.grid(row=0, column=2, padx=(6, 0))

        row = ttk.Frame(f)
        row.grid(row=2, column=0, sticky="ew")
        ttk.Label(row, text="Filter").grid(row=0, column=0, padx=(0, 6))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self._fill_scene_list())
        ttk.Entry(row, textvariable=self.filter_var, width=18).grid(row=0, column=1)
        ttk.Label(row, text="Put selected on button").grid(row=0, column=2, padx=(16, 6))
        for b in range(1, conv.MAX_SCENE_BUTTON + 1):
            ttk.Button(row, text=str(b), width=2, command=lambda b=b: self.toggle_pick(b)).grid(row=0, column=2 + b)
        ttk.Button(row, text="Clear", command=self.clear_picks).grid(row=0, column=10, padx=(6, 0))

        lists = ttk.Frame(f)
        lists.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        lists.columnconfigure(0, weight=1)
        lists.rowconfigure(0, weight=1)
        cols = ("button", "name", "fixture", "dmx")
        self.scene_list = ttk.Treeview(lists, columns=cols, show="headings", height=9)
        for c, text, w, anchor, stretch in (
            ("button", "Button", 60, "center", False),
            ("name", "QLC+ scene", 220, "w", True),
            ("fixture", "Fixture", 190, "w", True),
            ("dmx", "DMX channels", 110, "w", False),
        ):
            self.scene_list.heading(c, text=text, anchor=anchor)
            self.scene_list.column(c, width=w, anchor=anchor, stretch=stretch)
        self.scene_list.tag_configure("picked", font=("TkDefaultFont", 10, "bold"))
        self.scene_list.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lists, orient="vertical", command=self.scene_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.scene_list.configure(yscrollcommand=sb.set)
        for b in range(1, conv.MAX_SCENE_BUTTON + 1):
            self.scene_list.bind(f"<Key-{b}>", lambda e, b=b: self.toggle_pick(b))
        for key in ("<Key-0>", "<Delete>", "<BackSpace>"):
            self.scene_list.bind(key, lambda e: self.clear_picks())
        f.rowconfigure(3, weight=1)

        cols = ("button", "label", "chans", "name")
        self.table = ttk.Treeview(f, columns=cols, show="headings", height=8, selectmode="browse")
        for c, text, w, anchor in (
            ("button", "Button", 60, "center"),
            ("label", "Display", 90, "w"),
            ("chans", "Channels", 80, "center"),
            ("name", "Scenes on the box", 300, "w"),
        ):
            self.table.heading(c, text=text, anchor=anchor)
            self.table.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        self.table.bind("<Double-1>", self.edit_label)
        self.table.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self.msgs = tk.Text(f, height=4, wrap="word", relief="flat", background=self.cget("background"))
        self.msgs.tag_configure("error", foreground="#b00020")
        self.msgs.tag_configure("warning", foreground="#8a5a00")
        self.msgs.tag_configure("ok", foreground="#1b5e20")
        self.msgs.grid(row=5, column=0, sticky="ew", pady=(4, 10))
        self.msgs.configure(state="disabled")

        ttk.Label(f, text="2. Box", font=("TkDefaultFont", 11, "bold")).grid(row=6, column=0, sticky="w")
        row = ttk.Frame(f)
        row.grid(row=7, column=0, sticky="ew", pady=(4, 4))
        ttk.Label(row, text="USB port").grid(row=0, column=0, padx=(0, 6))
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", width=46)
        self.port_box.grid(row=0, column=1)
        self.port_box.bind("<<ComboboxSelected>>", lambda e: self._update_buttons())
        ttk.Button(row, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=(6, 0))

        self.safe_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            f,
            text="Fixtures are safe if DMX stops (off, unplugged, or tested)",
            variable=self.safe_var,
            command=self._update_buttons,
        ).grid(row=8, column=0, sticky="w", pady=(2, 6))

        row = ttk.Frame(f)
        row.grid(row=9, column=0, sticky="w")
        self.upload_btn = ttk.Button(row, text="Upload scenes", command=self.upload)
        self.upload_btn.grid(row=0, column=0)
        self.install_btn = ttk.Button(row, text="Install firmware + scenes…", command=self.install)
        self.install_btn.grid(row=0, column=1, padx=(8, 0))
        self.progress = ttk.Progressbar(row, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=2, padx=(12, 0))

        self.log = tk.Text(f, height=6, wrap="word", font=("TkFixedFont", 9))
        self.log.grid(row=10, column=0, sticky="nsew", pady=(8, 0))
        self.log.tag_configure("error", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#1b5e20")
        self.log.configure(state="disabled")
        f.rowconfigure(10, weight=1)

    def _build_info(self, f: ttk.Frame) -> None:
        ttk.Label(f, text="minidesk", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
        ttk.Label(f, text="Static DMX scene recall", foreground="#555").pack(anchor="w", pady=(0, 8))
        ttk.Label(f, text=INFO_TEXT, wraplength=290, justify="left").pack(anchor="w")

        ttk.Separator(f).pack(fill="x", pady=10)
        self._link(f, "minidesk on GitHub", REPO_URL)
        self._link(f, "Report a problem", ISSUES_URL)

        ttk.Separator(f).pack(fill="x", pady=10)
        ttk.Label(
            f,
            text="Scenes are made in QLC+, the free, open-source lighting control "
            "software by Massimo Callegari and contributors.",
            wraplength=290,
            justify="left",
        ).pack(anchor="w")
        self._link(f, "qlcplus.org", QLC_URL)
        ttk.Label(
            f,
            text="Flashing by esptool (Espressif). Filesystem by littlefs-python.",
            wraplength=290,
            justify="left",
            foreground="#555",
        ).pack(anchor="w", pady=(8, 0))

        fw = "bundled" if self.firmware else "not bundled"
        ttk.Label(f, text=f"Version: {build_info()}\nFirmware: {fw}", foreground="#555", justify="left").pack(
            anchor="w", side="bottom"
        )

    def _link(self, parent, text: str, url: str) -> None:
        lbl = ttk.Label(parent, text=text, foreground="#0b57d0", cursor="hand2")
        lbl.pack(anchor="w")
        lbl.bind("<Button-1>", lambda e: webbrowser.open(url))

    # --- workspace ------------------------------------------------------------

    def choose_file(self) -> None:
        initial = self.qxw.parent if self.qxw else Path.home()
        name = filedialog.askopenfilename(
            title="Choose a QLC+ workspace",
            initialdir=initial,
            filetypes=[("QLC+ workspace", "*.qxw"), ("All files", "*")],
        )
        if name:
            self.qxw = Path(name)
            self.reload_file()

    def reload_file(self) -> None:
        if not self.qxw:
            return
        self.path_var.set(str(self.qxw))
        try:
            self.scenes, errors = conv.list_scenes(self.qxw)
        except OSError as e:
            self.scenes, errors = [], [f"Can't read the file: {e}"]
        self._restore_picks()
        self._fill_scene_list()
        self._reconvert(load_errors=errors)

    def _restore_picks(self) -> None:
        """Saved picks for this workspace, matched by scene ID and name so an
        edited workspace never puts the wrong scene on a button. With none
        saved, scenes named "1 Tunnel" start on their button."""
        self.picks, self.labels, self.pick_notes = {}, {}, []
        by_id = {s.id: s for s in self.scenes}
        saved = load_picks(self.qxw) or {}
        for key, entry in saved.items():
            if not (key.isdigit() and 1 <= int(key) <= conv.MAX_SCENE_BUTTON and isinstance(entry, dict)):
                continue
            b = int(key)
            for item in entry.get("scenes", []):
                try:
                    sid, name = int(item[0]), str(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if sid in by_id and by_id[sid].name == name:
                    self.picks.setdefault(b, []).append(sid)
                    continue
                same_name = [s for s in self.scenes if s.name == name]
                if len(same_name) == 1:
                    self.picks.setdefault(b, []).append(same_name[0].id)
                else:
                    self.pick_notes.append(f"Button {b}: scene '{name}' is no longer in the workspace, so it was taken off")
            if isinstance(entry.get("label"), str) and b in self.picks:
                self.labels[b] = entry["label"]
        if self.picks:
            return
        for sc in self.scenes:
            if sc.button is None:
                continue
            if sc.button in self.picks:
                self.pick_notes.append(
                    f"'{sc.name}' is also numbered {sc.button}; only '{by_id[self.picks[sc.button][0]].name}' was put on it"
                )
                continue
            self.picks[sc.button] = [sc.id]

    def _save_picks(self) -> None:
        by_id = {s.id: s for s in self.scenes}
        save_picks(
            self.qxw,
            {
                str(b): {"label": self.labels.get(b), "scenes": [[sid, by_id[sid].name] for sid in ids]}
                for b, ids in sorted(self.picks.items())
                if ids
            },
        )

    def _buttons_of(self, sid: int) -> list[int]:
        return [b for b, ids in sorted(self.picks.items()) if sid in ids]

    def _fill_scene_list(self) -> None:
        selected = set(self.scene_list.selection())
        self.scene_list.delete(*self.scene_list.get_children())
        needle = self.filter_var.get().strip().lower()
        for sc in self.scenes:
            fixtures = ", ".join(f.name for f in sc.fixtures) or "(none)"
            if needle and needle not in sc.name.lower() and needle not in fixtures.lower():
                continue
            buttons = self._buttons_of(sc.id)
            iid = str(sc.id)
            dmx = ", ".join(f.dmx_range() for f in sc.fixtures)
            self.scene_list.insert(
                "",
                "end",
                iid=iid,
                values=(", ".join(map(str, buttons)), sc.name, fixtures, dmx),
                tags=("picked",) if buttons else (),
            )
            if iid in selected:
                self.scene_list.selection_add(iid)

    def toggle_pick(self, button: int) -> None:
        """Put the selected scenes on a button, or take them off if they're all on it already."""
        ids = [int(i) for i in self.scene_list.selection()]
        if not ids or self.busy:
            return
        current = self.picks.setdefault(button, [])
        if all(i in current for i in ids):
            current[:] = [i for i in current if i not in ids]
        else:
            current.extend(i for i in ids if i not in current)
        if not current:
            self.picks.pop(button)
            self.labels.pop(button, None)
        self._picks_changed()

    def clear_picks(self) -> None:
        ids = {int(i) for i in self.scene_list.selection()}
        if not ids or self.busy:
            return
        for b in list(self.picks):
            self.picks[b] = [i for i in self.picks[b] if i not in ids]
            if not self.picks[b]:
                self.picks.pop(b)
                self.labels.pop(b, None)
        self._picks_changed()

    def edit_label(self, event) -> None:
        item = self.table.identify_row(event.y)
        if not item or not item.isdigit() or int(item) not in self.picks or self.busy:
            return
        b = int(item)
        text = simpledialog.askstring(
            "Display label",
            f"What the box shows for button {b}: up to {conv.LABEL_MAX} characters, no '.'.\n"
            "Leave empty for the default.",
            initialvalue=self.table.set(item, "label"),
            parent=self,
        )
        if text is None:
            return
        if text.strip():
            self.labels[b] = text.strip()
        else:
            self.labels.pop(b, None)
        self._picks_changed()

    def _picks_changed(self) -> None:
        self._save_picks()
        self._fill_scene_list()
        self._reconvert()

    def _reconvert(self, load_errors: list[str] | None = None) -> None:
        self.table.delete(*self.table.get_children())
        if load_errors or not self.qxw:
            self.converted = None
            self._set_msgs([("error", f"Error: {e}") for e in load_errors or []])
            self._update_buttons()
            return
        assignments = [conv.Assignment(b, ids, self.labels.get(b)) for b, ids in self.picks.items()]
        try:
            self.converted = mf.convert(self.qxw, assignments)
        except OSError as e:
            self.converted = None
            self._set_msgs([("error", f"Can't read the file: {e}")])
            self._update_buttons()
            return
        c = self.converted
        on_box = {s["button"]: s for s in c.scenes}
        by_id = {s.id: s for s in self.scenes}
        for b in range(1, conv.MAX_SCENE_BUTTON + 1):
            if b in on_box:
                s = on_box[b]
                self.table.insert("", "end", iid=str(b), values=(b, s["label"], len(s["values"]), s["name"]))
            elif b in self.picks:
                names = " + ".join(by_id[i].name for i in self.picks[b])
                self.table.insert("", "end", iid=str(b), values=(b, "", "", f"{names}  (has errors)"))
            else:
                self.table.insert("", "end", iid=str(b), values=(b, "", "", "(empty: changes nothing)"))
        self.table.insert("", "end", iid="8", values=(8, "bLAnK", 0, "(always BLANK)"))

        msgs = [("error", f"Error: {e}") for e in c.errors]
        msgs += [("warning", note) for note in self.pick_notes]
        msgs += [("warning", f"Warning: {w}") for w in c.warnings]
        if c.json_text is not None:
            n = len(c.scenes)
            msgs.insert(0, ("ok", f"{n} button{'s' if n != 1 else ''} ready to upload."))
        else:
            msgs.insert(0, ("error", "Fix the errors (in QLC+ or the picks above), then Reload. Nothing will be uploaded."))
        self._set_msgs(msgs)
        self._update_buttons()

    def _set_msgs(self, msgs: list[tuple[str, str]]) -> None:
        self.msgs.configure(state="normal")
        self.msgs.delete("1.0", "end")
        for tag, text in msgs:
            self.msgs.insert("end", text + "\n", tag)
        self.msgs.configure(state="disabled", height=min(max(len(msgs), 2), 8))

    # --- ports ----------------------------------------------------------------

    def refresh_ports(self) -> None:
        try:
            self.ports = mf.list_ports()
        except Exception as e:  # pyserial backend trouble shouldn't kill the app
            self.ports = []
            self._log(f"Couldn't list USB ports: {e}", "error")
        labels = [f"{p.device} — {'minidesk (ESP32)' if p.is_esp else p.description}" for p in self.ports]
        self.port_box["values"] = labels
        current = self.port_var.get()
        if current not in labels:
            esp = [i for i, p in enumerate(self.ports) if p.is_esp]
            self.port_var.set(labels[esp[0]] if esp else (labels[0] if labels else ""))
        self._update_buttons()

    def selected_port(self) -> str | None:
        label = self.port_var.get()
        for p in self.ports:
            if label.startswith(p.device + " "):
                return p.device
        return None

    # --- actions --------------------------------------------------------------

    def _update_buttons(self) -> None:
        ready = (
            not self.busy
            and self.converted is not None
            and self.converted.json_text is not None
            and self.selected_port() is not None
            and self.safe_var.get()
        )
        self.upload_btn.configure(state="normal" if ready else "disabled")
        self.install_btn.configure(state="normal" if ready and self.firmware else "disabled")
        self.reload_btn.configure(state="normal" if self.qxw and not self.busy else "disabled")

    def upload(self) -> None:
        port, text = self.selected_port(), self.converted.json_text
        self._run(f"Uploading scenes to {port}", lambda log: mf.upload_scenes(port, text, log), port)

    def install(self) -> None:
        if not messagebox.askokcancel(
            "Install firmware",
            "This replaces the firmware on the box, then writes these scenes.\n\n"
            "Only needed for a new box or a firmware update. Continue?",
        ):
            return
        port, text, fw = self.selected_port(), self.converted.json_text, self.firmware
        self._run(f"Installing firmware and scenes on {port}", lambda log: mf.install_firmware(port, fw, text, log), port)

    def _run(self, title: str, job, port: str) -> None:
        self.busy = True
        self._update_buttons()
        self.progress.start(12)
        self._log(title + "…")
        expected = len(self.converted.scenes)

        def work():
            log = lambda m: self.events.put(("log", m))
            try:
                job(log)
                summary = mf.load_summary(mf.read_boot_report(port))
                self.events.put(("done", (summary, expected)))
            except mf.FlashError as e:
                self.events.put(("fail", str(e)))
            except Exception as e:  # never leave the UI stuck busy
                self.events.put(("fail", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._finish()
                    summary, expected = payload
                    if summary is None:
                        self._log("Done. Check the box's display shows the scene count, then BLANK.", "ok")
                    elif f"loaded {expected}," in summary and "rejected 0" in summary:
                        self._log(f"Done. Box reports: {summary}", "ok")
                    else:
                        self._log(f"Uploaded, but the box reports: {summary}. Expected {expected} scenes.", "error")
                elif kind == "fail":
                    self._finish()
                    self._log(f"Failed: {payload}", "error")
                    self._log(
                        "If the write stopped part-way the box shows FS Err and stays in BLANK. "
                        "Check the USB cable and port, then upload again."
                    )
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self) -> None:
        self.busy = False
        self.progress.stop()
        self._update_buttons()

    def _log(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()

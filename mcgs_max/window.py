"""User-window operations for MCGS.

Reading is pure parsing of the archive stored in ``WndUser.lbObjects``.
Creating goes through the workbench's ``新建窗口`` button, because that is the
only path MCGS 3.3.6 actually honours (the 3.5.1 window-clipboard format is
silently ignored by the older build).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import win32

MARKER = "MCGS-SET DOCUMENT"

# OLE variant types that appear in window settings
_OLE_FMT = {2: "<h", 3: "<i", 4: "<f", 5: "<d", 6: "<q", 7: "<d",
            10: "<i", 11: "<h", 16: "<b", 17: "<B", 18: "<H", 19: "<I",
            20: "<q", 21: "<Q"}

# property indices, verified against a new-window archive
PROP_ID = 0
PROP_NAME = 1
PROP_TITLE = 2
PROP_KIND = 3
PROP_WIDTH = 6
PROP_HEIGHT = 7
PROP_BG = 15
PROP_DESIGN_W = 19
PROP_DESIGN_H = 20


@dataclass
class WindowProps:
    id: int
    name: str
    title: str
    kind: str
    width: int
    height: int
    design_width: int
    design_height: int
    background: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "design_width": self.design_width,
            "design_height": self.design_height,
            "background": f"#{self.background & 0xFF:02X}"
                          f"{(self.background >> 8) & 0xFF:02X}"
                          f"{(self.background >> 16) & 0xFF:02X}",
        }


class _Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError(f"truncated at {self.pos}, need {n}")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return struct.unpack("<B", self.take(1))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def count(self) -> int:
        n = self.u16()
        return self.u32() if n == 0xFFFF else n

    def cstring(self) -> str:
        wide = False
        n = self.u8()
        if n == 0xFF:
            n = self.u16()
            if n == 0xFFFE:
                wide = True
                n = self.u8()
                if n == 0xFF:
                    n = self.u16()
                    if n == 0xFFFF:
                        n = self.u32()
            elif n == 0xFFFF:
                n = self.u32()
        raw = self.take(n * (2 if wide else 1))
        return raw.decode("utf-16le" if wide else "cp936", errors="replace")


def _read_variant(r: _Reader):
    vt = r.u16()
    if vt in (0, 1):
        return None
    if vt in _OLE_FMT:
        return struct.unpack(_OLE_FMT[vt], r.take(struct.calcsize(_OLE_FMT[vt])))[0]
    if vt == 8:
        size = r.u32()
        if size % 2:
            raise ValueError("odd BSTR length")
        return r.take(size).decode("utf-16le", errors="replace")
    raise ValueError(f"unsupported OLE variant {vt:#x}")


def parse_window(blob: bytes) -> WindowProps | None:
    """Decode the 21 window settings from a WndUser archive."""
    if not blob or MARKER.encode("utf-16le") not in blob[:80]:
        return None
    try:
        r = _Reader(blob)
        r.take(12)
        marker = r.cstring()
        if not marker.startswith(MARKER):
            return None
        control_count = r.u32()

        # settings follow the control records; walk past them
        for _ in range(control_count):
            r.u32()                     # type id
            size = r.count()
            r.take(size)

        core_size = r.u32()
        password_size = r.u32()
        r.take(password_size)
        core = r.take(core_size)

        c = _Reader(core)
        n = c.count()
        props = [_read_variant(c) for _ in range(n)]
        if len(props) <= PROP_DESIGN_H:
            return None

        kind = props[PROP_KIND] if isinstance(props[PROP_KIND], str) else ""
        return WindowProps(
            id=int(props[PROP_ID] or 0),
            name=str(props[PROP_NAME] or ""),
            title=str(props[PROP_TITLE] or "") if isinstance(props[PROP_TITLE], str) else "",
            kind=kind,
            width=int(props[PROP_WIDTH] or 0),
            height=int(props[PROP_HEIGHT] or 0),
            design_width=int(props[PROP_DESIGN_W] or 0),
            design_height=int(props[PROP_DESIGN_H] or 0),
            background=int(props[PROP_BG] or 0),
        )
    except Exception:
        return None


# ---------------------------------------------------------------- list
def _require_project(project: str | None = None):
    """Return (pid, frame, workbench, project_path, error).

    An open dialog does not block reading — the database on disk is unaffected
    by it. Only writes need the dialogs cleared, which `page._context` does.
    """
    processes = win32.list_processes()
    if not processes:
        return None, None, None, None, {
            "ok": False, "error_code": "NO_MCGS_PROCESS",
            "message": "没有正在运行的 McgsSetPro.exe。"}

    try:
        pid, path = win32.resolve_project(project)
    except win32.ProjectLookupError as exc:
        return None, None, None, None, {
            "ok": False, "error_code": exc.code, "message": exc.message,
            "available": exc.available}

    frame = win32.main_frame(pid)
    if frame is None:
        return pid, None, None, None, {
            "ok": False, "error_code": "NO_MAIN_WINDOW",
            "message": "找不到 MCGS 主窗口。"}

    workbench = win32.workbench_window(pid)
    if workbench is None:
        return pid, frame, None, None, {
            "ok": False, "error_code": "NO_WORKBENCH",
            "message": "找不到工作台窗口，请确认工程已打开。"}

    return pid, frame, workbench, path, None


def get_window(window: str, project: str) -> dict:
    """Full contents of one window: properties plus every decoded control.

    Both `window` and `project` are required: with more than one project open
    there is no safe way to guess which one a call means.
    """
    pid, frame, workbench, path, error = _require_project(project)
    if error:
        return error

    from . import builder, controls as controls_mod
    from . import project as project_mod

    info = project_mod.read_project(path)
    for w in info.windows:
        if w.name != window:
            continue

        props = parse_window(w.blob)
        controls = controls_mod.parse_controls(w.controls)
        by_index = {r["index"]: r for r in w.controls}

        from . import binding
        from . import labels as labels_mod
        prefixes, _channels = labels_mod._binding_context(path)

        for item in controls:
            rec = by_index.get(item.get("index"))
            if rec is None:
                continue
            if prefixes:
                found = binding.read_binding(rec["payload"], prefixes)
                if found:
                    item["variable"] = found
            if rec["type_id"] != 0x5025:
                continue
            if prefixes:
                upper = binding.read_max_binding(rec["payload"], prefixes)
                if upper:
                    item["max_variable"] = upper
            try:
                item["number_format"] = builder.read_input_format(rec["payload"])
            except (ValueError, controls_mod.FormatError):
                pass

        kinds: dict[str, int] = {}
        for c in controls:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        return {
            "ok": True,
            "project": path,
            "window": (props.as_dict() if props else {"name": w.name}),
            "control_count": w.control_count,
            "blob_bytes": w.blob_bytes,
            "kinds": kinds,
            "controls": controls,
        }

    return {
        "ok": False, "error_code": "WINDOW_NOT_FOUND",
        "message": f"没有名为 {window!r} 的窗口。",
        "available": [w.name for w in info.windows],
    }



"""Read a .MCP project file (Microsoft Access/Jet) without locking it.

MCGS keeps the database open while running, so the file is copied to a temp
location first. Only public, documented-by-observation structure is parsed; the
goal is to report what exists, not to mutate anything.
"""

from __future__ import annotations

import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import compat

MARKER = "MCGS-SET DOCUMENT"


@dataclass
class WindowInfo:
    name: str
    control_count: int
    blob_bytes: int
    controls: list[dict] = field(default_factory=list)
    blob: bytes = field(default=b"", repr=False)
    source: str = "WndUser"
    deleted: bool = False


@dataclass
class ProjectInfo:
    path: str
    exists: bool
    size: int = 0
    modified: str = ""
    readable: bool = False
    error: str = ""
    windows: list[WindowInfo] = field(default_factory=list)

    @property
    def name(self) -> str:
        return Path(self.path).stem if self.path else ""

    @property
    def control_count(self) -> int:
        return sum(w.control_count for w in self.windows)


def _read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    """MFC CString: optional 0xFF 0xFE 0xFF prefix, then length, then bytes."""
    wide = False
    n = data[pos]
    pos += 1
    if n == 0xFF:
        n = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if n == 0xFFFE:
            wide = True
            n = data[pos]
            pos += 1
            if n == 0xFF:
                n = struct.unpack_from("<H", data, pos)[0]
                pos += 2
                if n == 0xFFFF:
                    n = struct.unpack_from("<I", data, pos)[0]
                    pos += 4
        elif n == 0xFFFF:
            n = struct.unpack_from("<I", data, pos)[0]
            pos += 4
    raw = data[pos:pos + n * (2 if wide else 1)]
    pos += n * (2 if wide else 1)
    return raw.decode("utf-16le" if wide else "cp936", errors="replace"), pos


def parse_controls(blob: bytes) -> list[dict]:
    """Walk the window archive and summarise each stored object."""
    if len(blob) < 16 or MARKER.encode("utf-16le") not in blob[:64]:
        return []

    pos = 12
    marker, pos = _read_cstring(blob, pos)
    if not marker.startswith(MARKER):
        return []
    count = struct.unpack_from("<I", blob, pos)[0]
    pos += 4

    controls = []
    for index in range(min(count, 5000)):
        if pos + 4 > len(blob):
            break
        type_id = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        # length prefix: u16, or FFFF followed by u32
        n16 = struct.unpack_from("<H", blob, pos)[0]
        pos += 2
        if n16 == 0xFFFF:
            size = struct.unpack_from("<I", blob, pos)[0]
            pos += 4
        else:
            size = n16
        payload = blob[pos:pos + size]
        pos += size
        controls.append({"index": index, "type_id": type_id, "payload_bytes": size,
                         "rect": _rect_of(payload), "payload": payload})
    return controls


def _rect_of(payload: bytes) -> list[int] | None:
    """Rect lives in the common object header: skip version, style, optional link."""
    try:
        pos = 4
        flags = struct.unpack_from("<I", payload, pos)[0]
        pos += 8
        if flags & 0x100:
            pos += 8
            _, pos = _read_cstring(payload, pos)
        return list(struct.unpack_from("<4i", payload, pos))
    except Exception:
        return None


def read_project(path: str | Path, include_deleted: bool = False) -> ProjectInfo:
    """Read a project's windows.

    MCGS marks removed rows with ``iDeleteFlag = -1`` instead of removing them,
    so deleted windows are filtered out unless ``include_deleted`` is set.
    """
    path = Path(path)
    info = ProjectInfo(path=str(path), exists=path.exists())
    if not info.exists:
        info.error = "file not found"
        return info

    stat = path.stat()
    info.size = stat.st_size
    info.modified = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")

    access_parser = compat.require("access_parser")

    tmp = Path(tempfile.gettempdir()) / f"mcgs_max_read_{path.stem}.MCP"
    try:
        shutil.copy2(path, tmp)
    except OSError as exc:
        info.error = f"cannot copy project (in use?): {exc}"
        return info

    try:
        db = access_parser.AccessParser(str(tmp))
        for table, name_col, blob_col in (("WndUser", "strName", "lbObjects"),
                                          ("WndControl", "strName", "lbObjects"),
                                          ("WndDevice", "strWndDevName", "lbObjects")):
            try:
                data = db.parse_table(table)
            except Exception:
                continue
            if blob_col not in data or name_col not in data:
                continue
            deleted = data.get("iDeleteFlag")
            for i, blob in enumerate(data[blob_col]):
                if not blob:
                    continue
                flag = deleted[i] if deleted is not None else 0
                if not include_deleted and flag not in (0, None):
                    continue
                controls = parse_controls(blob)
                info.windows.append(WindowInfo(
                    name=str(data[name_col][i]),
                    control_count=len(controls),
                    blob_bytes=len(blob),
                    controls=controls,
                    blob=bytes(blob),
                    source=table,
                    deleted=bool(flag not in (0, None)),
                ))
        info.readable = True
    except Exception as exc:
        info.error = f"{type(exc).__name__}: {exc}"
    finally:
        tmp.unlink(missing_ok=True)

    return info

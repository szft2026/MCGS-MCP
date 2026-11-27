"""Read the device window: which PLC is configured and its channel table.

MCGS keeps the whole 设备窗口 as one blob in WndDevice.lbObjects. It is a
``MCGS-SET DOCUMENT`` archive whose payload is a run of wide strings: the parent
device, the child device, the driver path, then one pair per channel — the data
object name and the channel name (``设备0_`` + object).
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from . import compat


def _wide_strings(data: bytes) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    i = data.find(b"\xff\xfe\xff")
    while i >= 0:
        pos = i + 3
        if pos >= len(data):
            break
        n = data[pos]
        pos += 1
        if n == 0xFF:
            if pos + 2 > len(data):
                break
            n = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        raw = data[pos:pos + n * 2]
        try:
            text = raw.decode("utf-16le")
        except UnicodeDecodeError:
            text = ""
        if text:
            out.append((i, text))
        i = data.find(b"\xff\xfe\xff", i + 1)
    return out


def _register_kind(object_name: str) -> str:
    """Classify a channel by the register letter in its object name."""
    for prefix, kind in (("只读", "输入"), ("读写", "输出/中间"), ("", "其他")):
        if object_name.startswith(prefix):
            rest = object_name[len(prefix):]
            m = re.match(r"([A-Za-z]+)", rest)
            if m:
                return f"{kind} {m.group(1).upper()}"
            return kind
    return "其他"


def parse_devices(blob: bytes) -> dict:
    """Devices and channel pairs inside a device-window blob.

    The payload starts with the document header, then the parent device name and
    type, the child device name and type, the driver path, and then the channel
    table. Channel entries are pairs: the data object name followed by
    ``<device>_<object>``. The device name is whichever prefix the most strings
    share, which is stable even when names contain underscores themselves.
    """
    strings = _wide_strings(blob)
    if not strings:
        return {"devices": [], "strings": 0}

    texts = [t for _o, t in strings]

    # 找出被最多字符串当作前缀的那个名字
    counts: dict[str, int] = {}
    for text in texts:
        if "_" not in text:
            continue
        prefix = text.split("_", 1)[0]
        if not prefix:
            continue
        counts[prefix] = sum(1 for t in texts if t.startswith(prefix + "_"))
    channel_prefix = max(counts, key=counts.get) if counts else None
    if channel_prefix and counts[channel_prefix] < 2:
        channel_prefix = None

    pairs: list[dict] = []
    if channel_prefix:
        for idx, text in enumerate(texts):
            if not text.startswith(channel_prefix + "_"):
                continue
            obj = texts[idx - 1] if idx else ""
            if obj.startswith(channel_prefix + "_"):
                continue
            pairs.append({
                "object": obj,
                "channel": text,
                "register": _register_kind(obj),
            })

    # 头部顺序取决于有没有父设备：
    #   有父设备 [父名, 父类型, 设备名, 设备类型, 驱动路径, ...]
    #   无父设备 [设备名, 设备类型, 驱动路径, ...]
    # 用驱动路径（.ui）当锚点，它前面两个就是设备名和类型。
    body = [t for t in texts if not t.startswith("MCGS-SET DOCUMENT")]
    driver_index = next(
        (i for i, t in enumerate(body) if t.lower().endswith(".ui")), None)

    if driver_index is not None and driver_index >= 2:
        device_name = body[driver_index - 2]
        device_type = body[driver_index - 1]
        driver = body[driver_index]
        has_parent = driver_index >= 4
        parent_name = body[0] if has_parent else ""
        parent_type = body[1] if has_parent else ""
    else:
        # 没有 .ui 可定位时退回旧布局
        parent_name = body[0] if len(body) > 0 else ""
        parent_type = body[1] if len(body) > 1 else ""
        device_name = body[2] if len(body) > 2 else (channel_prefix or "")
        device_type = body[3] if len(body) > 3 else ""
        driver = ""

    ipv4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
    addresses = [t for t in texts if ipv4.match(t)]

    devices = []
    if device_name:
        devices.append({
            "name": device_name,
            "type": device_type,
            "driver": driver,
            "parent": {"name": parent_name, "type": parent_type},
            "addresses": addresses,
            "channel_count": len(pairs),
            "channels": pairs,
        })

    return {"devices": devices, "strings": len(strings)}


def read_devices(project_path: str) -> dict:
    """Every device configured in the project, with its channel table."""
    access_parser = compat.require("access_parser")

    tmp = Path(tempfile.gettempdir()) / f"mcgs-dev-{Path(project_path).stem}.mcp"
    shutil.copy2(project_path, tmp)
    try:
        db = access_parser.AccessParser(str(tmp))
        table = db.parse_table("WndDevice")
    finally:
        tmp.unlink(missing_ok=True)

    blobs = table.get("lbObjects") or []
    deleted = table.get("iDeleteFlag") or []
    names = table.get("strWndDevName") or []

    results = []
    for idx, blob in enumerate(blobs):
        if idx < len(deleted) and deleted[idx]:
            continue
        if not blob:
            continue
        parsed = parse_devices(bytes(blob))
        entry = {
            "window": names[idx] if idx < len(names) else "",
            "blob_bytes": len(blob),
            **parsed,
        }
        results.append(entry)

    return {
        "ok": True,
        "project": project_path,
        "device_windows": results,
    }

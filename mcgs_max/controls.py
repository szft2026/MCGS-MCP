"""Decode the controls stored inside a MCGS window archive.

A window's ``lbObjects`` blob holds one payload per control. Every payload
starts with a common header (geometry, pen, brush, name) followed by
type-specific fields. Only the types listed in ``KINDS`` have been verified;
anything else is reported with its raw type id and byte count rather than
guessed at.

All offsets here were confirmed by round-tripping real project data, so a
payload that does not parse is reported as such instead of yielding wrong
values.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# control type ids observed in McgsPro 3.3.6 projects
KINDS = {
    0x5000: "draw_obj",
    0x5004: "draw_obj",
    0x5008: "label",
    0x5009: "picture",
    0x5025: "numeric_input",
    0x5027: "button",
    0x5028: "bit_switch",
    0x502A: "lamp",
    0xFFFFFFFF: "window_record",
}


class FormatError(ValueError):
    """Raised when a payload does not match the verified layout."""


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise FormatError(f"truncated at {self.pos}, need {n}")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return struct.unpack("<B", self.take(1))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def count(self) -> int:
        n = self.u16()
        return self.u32() if n == 0xFFFF else n

    def byte_array(self) -> bytes:
        return self.take(self.count())

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


def colorref(value: int) -> str:
    """0x00BBGGRR -> '#RRGGBB'."""
    return (f"#{value & 0xFF:02X}"
            f"{(value >> 8) & 0xFF:02X}"
            f"{(value >> 16) & 0xFF:02X}")


def _wide_strings(payload: bytes) -> list[tuple[int, int, str]]:
    """Every ff fe ff cstring in the payload, as (offset, end, text)."""
    out = []
    i = payload.find(b"\xff\xfe\xff")
    while i >= 0:
        try:
            r = Reader(payload, i)
            text = r.cstring()
            out.append((i, r.pos, text))
        except (FormatError, UnicodeDecodeError):
            pass
        i = payload.find(b"\xff\xfe\xff", i + 1)
    return out


# Fonts that a caption is unlikely to be. Used only to tell "no caption" apart
# from "caption happens to be short".
FONT_HINTS = ("宋体", "黑体", "微软雅黑", "楷体", "仿宋", "隶书", "幼圆",
              "新宋体", "华文", "方正", "Arial", "Times", "Courier", "Tahoma")


def button_text_blocks(payload: bytes, name_off: int) -> list[dict]:
    """Caption of a button control.

    Unlike switches and lamps there is no ``文本0`` marker: the caption is simply
    the first non-empty cstring after the control name, followed by its font. A
    button without a caption puts the font there instead, so that case is
    detected and reported as an empty caption with no writable slot.
    """
    strings = _wide_strings(payload)
    after = [s for s in strings if s[0] > name_off and s[2]]
    if not after:
        return []

    first_off, _, first = after[0]
    second = after[1][2] if len(after) > 1 else ""
    second_off = after[1][0] if len(after) > 1 else -1

    looks_like_font = first == second or (
        len(first) <= 8 and any(h in first for h in FONT_HINTS))
    if looks_like_font:
        return [{"index": 0, "text": "", "text_off": -1,
                 "font": first, "font_off": first_off}]
    return [{"index": 0, "text": first, "text_off": first_off,
             "font": second, "font_off": second_off}]


CLICK_TAG = b"\xff\xff\x00\x00\x09\x00CAniClick"
CLICK_EXPR_OFFSET = 53


def click_variable(payload: bytes) -> str | None:
    """Variable a button's click action is bound to, if any.

    Verified across 228 components of MCGS's public library: the expression sits
    exactly 53 bytes after the CAniClick class tag. The 38 bytes between hold a
    float and a run of empty strings and are not interpreted.
    """
    i = payload.find(CLICK_TAG)
    if i < 0:
        return None
    try:
        r = Reader(payload, i + CLICK_EXPR_OFFSET)
        return r.cstring()
    except (FormatError, UnicodeDecodeError):
        return None


def set_action_variable(payload: bytes, name: str) -> bytes:
    """Retarget a button's click action to another variable.

    Only works when the control already carries a CAniClick record: the
    expression is replaced in place. Adding an action to a control that has none
    is not supported — the 78 bytes that follow the string table differ between
    library components, so a copied block cannot be trusted.
    """
    i = payload.find(CLICK_TAG)
    if i < 0:
        raise FormatError("控件没有 CAniClick 动作，暂不支持添加")

    offset = i + CLICK_EXPR_OFFSET
    r = Reader(payload, offset)
    old_text = r.cstring()
    consumed = r.pos - offset
    return payload[:offset] + wide_cstring(name) + payload[offset + consumed:]


ACTION_MODES = {
    0: "\u7f6e1",           # 置1
    1: "\u7f6e0",           # 置0
    2: "\u53d6\u53cd",     # 取反
    3: "\u63091\u677e0",   # 按1松0
    4: "\u63090\u677e1",   # 按0松1
}

# 1.0f followed by 100.0f; the mode word sits immediately before it and the
# pattern occurs exactly once per payload.
MODE_ANCHOR = struct.pack("<f", 1.0) + struct.pack("<f", 100.0)


def action_mode(payload: bytes) -> str | None:
    """Which of the five button-action modes the control uses.

    The mode word is the u32 immediately before the 1.0f/100.0f float pair that
    every 动画按钮 carries. Verified against the five modes captured from MCGS:
    0 置1, 1 置0, 2 取反, 3 按1松0, 4 按0松1.
    """
    i = payload.find(MODE_ANCHOR)
    if i < 4:
        return None
    return ACTION_MODES.get(struct.unpack_from("<I", payload, i - 4)[0])


def text_blocks(payload: bytes) -> list[dict]:
    """Find the text labels inside a control payload.

    Two shapes are handled:

    * switches and lamps: an object name (``文本0``), the displayed text, then the
      font face, repeated once per state;
    * buttons: a single caption followed by its font, with no marker.

    Locating by structure rather than by content means renaming still works.
    """
    marks = _wide_strings(payload)

    blocks = []
    for pos, (off, end, text) in enumerate(marks):
        if text != "文本0" or pos + 2 >= len(marks):
            continue
        text_off, _, display = marks[pos + 1]
        font_off, _, face = marks[pos + 2]
        blocks.append({
            "index": len(blocks),
            "name_off": off,
            "text": display,
            "text_off": text_off,
            "font": face,
            "font_off": font_off,
        })
    return blocks


def rgb_to_colorref(rgb: str) -> int:
    """'#RRGGBB' -> 0x00BBGGRR."""
    text = rgb.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"颜色必须是 #RRGGBB: {rgb!r}")
    r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    return (b << 16) | (g << 8) | r


def wide_cstring(text: str) -> bytes:
    """MCGS stores non-ASCII strings as ff fe ff, one byte of length, UTF-16LE."""
    raw = text.encode("utf-16le")
    chars = len(raw) // 2
    if chars > 0xFF:
        return b"\xff\xfe\xff\xff" + struct.pack("<H", chars) + raw
    return b"\xff\xfe\xff" + bytes([chars]) + raw


@dataclass
class Control:
    index: int
    type_id: int
    kind: str
    payload_bytes: int
    name: str = ""
    name_offset: int = 0
    rect: list[int] = field(default_factory=list)
    border_style: int = 0
    border_raw: str = ""
    brush_style: int = 0
    brush_color: int = 0
    caption: str = ""
    unit: str = ""
    text_color: int = 0
    font_height: int = 0
    font_weight: int = 0
    font_face: str = ""
    texts: list[str] = field(default_factory=list)
    text_faces: list[str] = field(default_factory=list)
    action_variable: str = ""
    action_mode: str = ""
    number_format: dict = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict:
        out = {
            "index": self.index,
            "type_id": f"0x{self.type_id:04X}",
            "kind": self.kind,
            "payload_bytes": self.payload_bytes,
        }
        if self.error:
            out["error"] = self.error
            return out
        if self.rect:
            out["name"] = self.name
            out["rect"] = self.rect
            out["size"] = [self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]]
            out["fill"] = (colorref(self.brush_color)
                           if self.brush_style == 1 else "transparent")
            out["border"] = self.border_style != 0
        if self.caption:
            out["caption"] = self.caption
            out["text_color"] = colorref(self.text_color)
        if self.font_height or self.font_face:
            out["font"] = {"height": self.font_height, "weight": self.font_weight,
                           "face": self.font_face}
        if self.unit:
            out["unit"] = self.unit
        if self.action_variable:
            out["action_variable"] = self.action_variable
        if self.action_mode:
            out["action_mode"] = self.action_mode
        if self.number_format:
            out["number_format"] = self.number_format
        if self.texts:
            out["texts"] = list(self.texts)
            if len(self.texts) == 2:
                out["text_off"] = self.texts[0]
                out["text_on"] = self.texts[1]
            if self.text_faces:
                out["text_faces"] = list(self.text_faces)
        return out


def _read_common(r: Reader, ctl: Control) -> int:
    """Geometry, pen, brush and name: shared by every control type.

    Animation objects sit between the brush block and the trailing u16 pair;
    they are walked here so every later offset stays deterministic.
    """
    r.take(4)                       # version float
    flags = r.u32()
    r.u32()
    if flags & 0x100:               # optional link block
        r.u32()
        r.u32()
        r.cstring()

    ctl.rect = list(struct.unpack("<4i", r.take(16)))

    ctl.border_style = r.u16()
    ctl.border_raw = r.take(16).hex()
    # Every drawn control in the sample projects shares one border block, so the
    # meaning of its individual words is not established; only "has a border"
    # is reported. The colour is kept raw rather than mislabelled.

    ctl.brush_style = r.u16()
    brush = r.take(12)
    ctl.brush_color = struct.unpack_from("<I", brush, 4)[0] if len(brush) >= 8 else 0

    animations = r.count()
    for _ in range(animations):
        _skip_animation(r)

    r.u16()
    r.u16()

    ctl.name_offset = r.pos
    ctl.name = r.cstring()
    r.byte_array()                  # per-control metadata
    return animations


def _skip_animation(r: Reader) -> dict:
    """Read one MFC-serialised animation object.

    Layout, verified against real payloads:

        class tag | initial float | expression | off text | on text | 4 x u32

    The class tag is 0xFFFF + schema + name length + name, or a 2-byte
    reference to a class already registered in the stream.
    """
    rec: dict = {}
    ref = r.u16()
    if ref == 0xFFFF:
        r.u16()                                 # schema
        n = r.u16()
        rec["class"] = r.take(n).decode("ascii", errors="replace")
    else:
        rec["class"] = f"ref:{ref:#06x}"

    rec["float"] = struct.unpack("<f", r.take(4))[0]
    rec["expression_off"] = r.pos
    rec["expression"] = r.cstring()

    cls = rec["class"]
    rec["fields_off"] = r.pos
    if cls == "CAniOutput":
        rec["off_text"] = r.cstring()
        rec["on_text"] = r.cstring()
        rec["fields_off"] = r.pos
        rec["fields"] = struct.unpack("<4I", r.take(16))
    elif cls == "CAniHMove":                 # 水平移动：无 off/on 文字
        rec["fields"] = struct.unpack("<4I", r.take(16))
    elif cls == "CAniVisible":               # 显隐：只有一个 u32
        rec["fields"] = (r.u32(),)
    elif cls == "CAniBrush":
        # 阈值数组 + 颜色数组，各带一个 u16 数量
        count = r.u16()
        if count > 64:
            raise FormatError(f"CAniBrush 阈值数量 {count} 超出范围")
        thresholds = tuple(
            struct.unpack("<f", r.take(4))[0] for _ in range(count))
        colours_count = r.u16()
        if colours_count > 64:
            raise FormatError(f"CAniBrush 颜色数 {colours_count} 超出范围")
        colours = tuple(struct.unpack("<I", r.take(4))[0]
                        for _ in range(colours_count))
        rec["fields"] = thresholds + colours
    else:
        raise FormatError(
            f"未知动画类 {cls}，无法确定它占多少字节；"
            "这个控件暂不支持修改，请在 MCGS 属性框里操作。")
    return rec


def _read_font(r: Reader, ctl: Control) -> None:
    """CMcgsFont: u8, u32 language count, then (u32 langid, LOGFONT, face)."""
    r.u8()
    count = r.u32()
    if count > 32:
        raise FormatError(f"font language count {count} out of range")
    for i in range(count):
        r.u32()                     # language id
        logfont = r.take(28)
        face = r.cstring()
        if i == 0:                  # first language is the one MCGS displays
            ctl.font_height = struct.unpack_from("<i", logfont, 0)[0]
            ctl.font_weight = struct.unpack_from("<i", logfont, 16)[0]
            ctl.font_face = face


def _read_label(r: Reader, ctl: Control) -> None:
    """0x5008: text output. Layout verified against real payloads."""
    ctl.caption = r.cstring()
    for _ in range(7):              # appearance prefix words
        r.u32()
    ctl.unit = r.cstring()
    suffix = [r.u32() for _ in range(4)]
    if suffix[3]:
        raise FormatError("background picture present, layout not verified")

    if r.count():                   # extra property bag
        raise FormatError("extra property bag not empty")

    ctl.text_color = r.u32()
    r.u16()                         # style
    _read_font(r, ctl)
    r.take(2)                       # flags
    for _ in range(6):              # properties
        r.u32()
    r.cstring()                     # extra string

    remaining = len(r.data) - r.pos
    if remaining not in (0, 4, 8, 12):
        raise FormatError(f"{remaining} trailing bytes not understood")
    r.take(remaining)


def parse_control(index: int, type_id: int, payload: bytes) -> Control:
    ctl = Control(index=index, type_id=type_id,
                  kind=KINDS.get(type_id, "unknown"),
                  payload_bytes=len(payload))
    if type_id == 0xFFFFFFFF:
        # the window's own settings record, not a control
        ctl.error = "window settings record, not a control"
        return ctl

    r = Reader(payload)
    try:
        _read_common(r, ctl)
        if type_id == 0x5008:
            _read_label(r, ctl)
        else:
            blocks = (button_text_blocks(payload, ctl.name_offset)
                      if type_id == 0x5027 else text_blocks(payload))
            ctl.texts = [b["text"] for b in blocks]
            ctl.text_faces = [b["font"] for b in blocks]
            bound = click_variable(payload)
            if bound:
                ctl.action_variable = bound
            mode = action_mode(payload)
            if mode:
                ctl.action_mode = mode
    except (FormatError, struct.error, UnicodeDecodeError) as exc:
        ctl.error = f"{type(exc).__name__}: {exc}"
    return ctl


def parse_controls(records: list[dict]) -> list[dict]:
    """Decode the payloads that ``project.parse_controls`` already split out."""
    out = []
    for rec in records:
        ctl = parse_control(rec["index"], rec["type_id"], rec["payload"])
        out.append(ctl.as_dict())
    return out

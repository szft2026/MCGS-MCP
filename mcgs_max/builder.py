"""Build MCGS controls from templates MCGS itself produced.

Templates live in ``templates.json`` and were captured by placing each control in
the editor and reading it back out of the project database. A template carries
whatever internal structure that MCGS build expects — font, border, event
metadata, animations, embedded images — so building a control means copying one
and rewriting the fields the caller cares about.

Layout of a control payload, all offsets located by walking the record:

    version f32 | flags u32 | ? u32 | [link block] | rect 16
    pen u16 + 16 | brush u16 + 12 | animations | u16 u16 | name | metadata
    ... then type-specific fields

Only labels continue with text fields:

    caption | 7 x u32 | unit | 4 x u32 | [picture] | property bag
    colour u32 | style u16 | font | flags 2 | 6 x u32 | extra | tail

An output label adds a CAniOutput animation before the trailing u16 pair. The
last four words of that animation start with [mode, decimal_places]: 2 is a
string variable, 1 is numeric. Getting this wrong is reported by MCGS as
显示输出: 类型不匹配.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .controls import (FormatError, Reader, _read_font, _skip_animation,
                       _wide_strings, button_text_blocks, rgb_to_colorref,
                       text_blocks, wide_cstring)

_TEMPLATE_FILE = Path(__file__).resolve().parent / "templates.json"
_TEMPLATES: dict[str, dict] = json.loads(_TEMPLATE_FILE.read_text(encoding="utf-8"))

TYPE_LABEL = 0x5008
TYPE_PICTURE = 0x5009
TYPE_INPUT = 0x5025
TYPE_BUTTON = 0x5027
TYPE_SWITCH = 0x5028
TYPE_LAMP = 0x502A

# clipboard class name per native control type
TYPE_CLASS = {
    0x5000: "CDrawObj",      # MCGS 保存后会把 0x5004 规范成 0x5000
    0x5004: "CDrawRect",
    0x5008: "CDrawLabel",
    0x5009: "CDrawBitmap",
    0x5025: "CDrawEdit",
    0x5027: "CDrawButton",
    0x5028: "CDrawMulBtn",
    0x502A: "CDrawMulDis",
    0x502D: "CDrawPic",
    0x5031: "CDrawSliderFill",
    0x5032: "CDrawRotateTbl",
    0x503D: "CDrawDll",
}

KIND_NAMES = {
    "label": TYPE_LABEL,
    "button": TYPE_BUTTON,
    "output": TYPE_LABEL,
    "bit_switch": TYPE_SWITCH,
    "lamp": TYPE_LAMP,
    "numeric_input": TYPE_INPUT,
}

MODE_NUMERIC = 1
MODE_STRING = 2

# MCGS system variables whose type is fixed; user variables must state it.
STRING_VARS = {"$Date", "$Time"}


def template(key) -> bytes:
    """Template payload by type id or by name."""
    entry = _TEMPLATES.get(str(key))
    if entry is None:
        raise ValueError(f"没有 {key!r} 的模板")
    return bytes.fromhex(entry["hex"])


def template_types() -> dict[int, str]:
    """Type id -> template name, for everything we can build from scratch."""
    out = {}
    for entry in _TEMPLATES.values():
        out.setdefault(entry["type_id"], entry["name"])
    return out


LABEL_TEMPLATE = template(TYPE_LABEL)
OUTPUT_TEMPLATE = template("output")


@dataclass
class Layout:
    """Byte offsets of the fields the builders rewrite."""

    rect: int = 0
    brush: int = 0
    name: int = 0
    flag_offset: int = 0
    metadata_count: int = 0
    metadata_len: int = 0
    after_metadata: int = 0
    caption: int = 0
    colour: int = 0
    font: int = 0
    animations: list[dict] = field(default_factory=list)


def walk_common(payload: bytes) -> Layout:
    """Header shared by every control type: geometry, pen, brush, name."""
    r = Reader(payload)
    out = Layout()

    r.take(4)                              # version
    flags = r.u32()
    r.u32()
    if flags & 0x100:
        r.u32()
        r.u32()
        r.cstring()

    out.rect = r.pos
    r.take(16)
    r.take(2 + 16)                         # pen style + pen
    out.brush = r.pos
    r.take(2 + 12)                         # brush style + brush

    for _ in range(r.count()):
        out.animations.append(_skip_animation(r))

    r.u16()
    out.flag_offset = r.pos          # second u16 of the common header
    r.u16()

    out.name = r.pos
    r.cstring()
    out.metadata_count = r.pos             # length prefix of the metadata blob
    out.metadata_len = r.count()
    r.take(out.metadata_len)
    out.after_metadata = r.pos
    return out


def walk(payload: bytes) -> Layout:
    """Label layout: the common header plus the text fields."""
    lay = walk_common(payload)
    r = Reader(payload, lay.after_metadata)

    lay.caption = r.pos
    r.cstring()
    r.take(7 * 4)                          # appearance prefix
    r.cstring()                            # unit
    suffix = struct.unpack("<4I", r.take(16))
    if suffix[3]:
        raise FormatError("background picture present, layout not handled")

    if r.count():                          # property bag
        raise FormatError("extra property bag not empty")

    lay.colour = r.pos
    r.u32()
    r.u16()                                # style
    lay.font = r.pos
    _read_font(r, _FontSink())
    return lay


class _FontSink:
    """_read_font expects a Control; only the offsets matter here."""

    font_height = 0
    font_weight = 0
    font_face = ""


def _replace_at(payload: bytes, offset: int, text: str) -> bytes:
    """Replace the wide cstring that starts at `offset`."""
    r = Reader(payload, offset)
    start = r.pos
    r.cstring()
    consumed = r.pos - start
    return payload[:offset] + wide_cstring(text) + payload[offset + consumed:]


def set_rect(payload: bytes, rect) -> bytes:
    if len(rect) != 4 or rect[0] >= rect[2] or rect[1] >= rect[3]:
        raise ValueError(f"矩形不合法: {rect}")
    out = bytearray(payload)
    struct.pack_into("<4i", out, walk_common(payload).rect, *rect)
    return bytes(out)


def set_name(payload: bytes, text: str) -> bytes:
    return _replace_at(payload, walk_common(payload).name, text)


def set_caption(payload: bytes, text: str) -> bytes:
    return _replace_at(payload, walk(payload).caption, text)


def set_fill(payload: bytes, rgb: str | None) -> bytes:
    brush = walk_common(payload).brush
    out = bytearray(payload)
    if rgb is None:
        struct.pack_into("<H", out, brush, 0)
    else:
        struct.pack_into("<H", out, brush, 1)
        struct.pack_into("<I", out, brush + 6, rgb_to_colorref(rgb))
    return bytes(out)


def set_text_color(payload: bytes, rgb: str) -> bytes:
    out = bytearray(payload)
    struct.pack_into("<I", out, walk(payload).colour, rgb_to_colorref(rgb))
    return bytes(out)


def set_font(payload: bytes, size_px: int | None = None,
             face: str | None = None, weight: int | None = None) -> bytes:
    """Rewrite the first language's LOGFONT height/weight and its face name."""
    logfont = walk(payload).font + 5 + 4      # u8 + u32 count, then language id
    out = bytearray(payload)
    if size_px is not None:
        struct.pack_into("<i", out, logfont, -abs(int(size_px)))
    if weight is not None:
        struct.pack_into("<i", out, logfont + 16, int(weight))
    result = bytes(out)
    if face is not None:
        result = _replace_at(result, logfont + 28, face)
    return result


def set_border(payload: bytes, rgb: str | None, width: int | None = None) -> bytes:
    """Set the border colour and/or line width.

    The pen block sits immediately before the brush: a u16 style followed by four
    words. Verified by pasting one variant per word and looking at the canvas:

        +0  pen style (6 for the default border)
        +4  line width in pixels (default 2; 255 floods the whole box)
        +8  unused in the observed records
        +12 border colour as COLORREF (default 0 = black)

    Setting the style word to 0 removes the border entirely.
    """
    brush = walk_common(payload).brush
    style_off = brush - 18
    data_off = brush - 16
    out = bytearray(payload)
    if rgb is None:
        struct.pack_into("<H", out, style_off, 0)
    else:
        struct.pack_into("<H", out, style_off, 1)
        struct.pack_into("<I", out, data_off + 12, rgb_to_colorref(rgb))
        if width is not None:
            struct.pack_into("<I", out, data_off + 4, max(1, int(width)))
    return bytes(out)


def set_output(payload: bytes, variable: str, mode: int, decimals: int) -> bytes:
    """Point an output animation at `variable` with the given mode and precision."""
    anim = next((a for a in walk_common(payload).animations
                 if a["class"] == "CAniOutput"), None)
    if anim is None:
        raise FormatError("控件没有 CAniOutput 动画")

    out = bytearray(payload)
    struct.pack_into("<4I", out, anim["fields_off"],
                     mode, decimals, anim["fields"][2], anim["fields"][3])
    result = bytes(out)

    anim2 = next(a for a in walk_common(result).animations
                 if a["class"] == "CAniOutput")
    return _replace_at(result, anim2["expression_off"], variable)


def infer_mode(variable: str) -> int:
    return MODE_STRING if variable in STRING_VARS else MODE_NUMERIC


def _fill_value(fill: str | None):
    """'none' means no fill; anything else is a colour."""
    if fill is None:
        return None
    if fill.strip().lower() == "none":
        return None
    return fill


def make_label(name: str, caption: str, rect, *, size_px: int | None = None,
               face: str | None = None, weight: int | None = None,
               text_color: str | None = None, fill: str | None = None) -> bytes:
    payload = set_name(LABEL_TEMPLATE, name)
    payload = set_caption(payload, caption)
    payload = set_rect(payload, rect)
    if size_px or face or weight:
        payload = set_font(payload, size_px, face, weight)
    if text_color:
        payload = set_text_color(payload, text_color)
    if fill is not None:
        payload = set_fill(payload, _fill_value(fill))
    return payload


def make_output(name: str, rect, variable: str, *, mode: int | None = None,
                decimals: int = 0, size_px: int | None = None,
                face: str | None = None, weight: int | None = None,
                text_color: str | None = None, fill: str | None = None) -> bytes:
    payload = set_name(OUTPUT_TEMPLATE, name)
    payload = set_rect(payload, rect)
    payload = set_output(payload, variable,
                         mode if mode is not None else infer_mode(variable),
                         decimals)
    if size_px or face or weight:
        payload = set_font(payload, size_px, face, weight)
    if text_color:
        payload = set_text_color(payload, text_color)
    if fill is not None:
        payload = set_fill(payload, _fill_value(fill))
    return payload


def _blocks(payload: bytes) -> list[dict]:
    """Text blocks of any supported type: switch/lamp markers, else a caption."""
    blocks = text_blocks(payload)
    if blocks:
        return blocks
    return button_text_blocks(payload, walk_common(payload).name)


def _patch_count(payload: bytes, offset: int, value: int) -> bytes:
    """Rewrite a count field, keeping the u16 / FFFF+u32 form the record uses."""
    out = bytearray(payload)
    wide = struct.unpack_from("<H", out, offset)[0] == 0xFFFF
    if wide:
        struct.pack_into("<H", out, offset, 0xFFFF)
        struct.pack_into("<I", out, offset + 2, value)
    else:
        if value >= 0xFFFF:
            raise ValueError("metadata 太长，无法用 u16 表示")
        struct.pack_into("<H", out, offset, value)
    return bytes(out)


def replace_cstring_at(payload: bytes, offset: int, text: str) -> bytes:
    """Replace one wide string in place, fixing the length fields that cover it."""
    r = Reader(payload, offset)
    r.cstring()
    consumed = r.pos - offset
    new = wide_cstring(text)
    delta = len(new) - consumed

    out = payload[:offset] + new + payload[offset + consumed:]
    if delta:
        lay = walk_common(payload)
        meta_start = lay.after_metadata - lay.metadata_len
        if meta_start <= offset < lay.after_metadata:
            out = _patch_count(out, lay.metadata_count, lay.metadata_len + delta)
            out = _patch_nested_lengths(out, lay, delta)
    return out


def replace_global(payload: bytes, old_text: str, new_text: str,
                   limit: int = 4) -> bytes:
    """Replace every copy of a wide string and keep the metadata length correct.

    A button keeps its caption and font in two places — inside the metadata blob
    and again after it. Both copies must be rewritten or MCGS rejects the
    payload, and because the metadata blob carries its own length prefix, that
    prefix has to be corrected whenever a copy inside it changes size.
    """
    old = wide_cstring(old_text)
    new = wide_cstring(new_text)
    count = payload.count(old)
    if count == 0:
        return payload
    if count > limit:
        raise ValueError(f"{old_text!r} 出现 {count} 次，超过预期，拒绝替换")

    lay = walk_common(payload)
    meta_start = lay.after_metadata - lay.metadata_len
    meta_end = lay.after_metadata
    delta = 0
    i = payload.find(old)
    while i >= 0:
        if meta_start <= i < meta_end:
            delta += len(new) - len(old)
        i = payload.find(old, i + 1)

    result = payload.replace(old, new)
    if delta:
        result = _patch_count(result, lay.metadata_count, lay.metadata_len + delta)
        result = _patch_nested_lengths(result, lay, delta)
    return result


# Some controls nest a second length inside the metadata blob: two u16 words at
# +10 and +12 that both measure the remaining bytes. Growing a caption without
# adjusting them makes MCGS reject the payload, even though the outer length is
# correct. Verified on a button: outer-only works up to +1 character, outer+nested
# works at any length.
_NESTED_OFFSETS = (10, 12)


def _patch_nested_lengths(payload: bytes, lay, delta: int) -> bytes:
    """Bump the nested metadata lengths when the record actually has them."""
    if lay.metadata_len < 16:
        return payload

    start = lay.after_metadata - lay.metadata_len
    first = struct.unpack_from("<H", payload, start + _NESTED_OFFSETS[0])[0]
    # the nested block starts after a 12-byte prefix, so the sizes must add up
    if first + 12 != lay.metadata_len:
        return payload

    out = bytearray(payload)
    for rel in _NESTED_OFFSETS:
        off = start + rel
        value = struct.unpack_from("<H", out, off)[0]
        if value + delta < 0 or value + delta >= 0xFFFF:
            raise ValueError("嵌套长度越界")
        struct.pack_into("<H", out, off, value + delta)
    return bytes(out)


def set_text(payload: bytes, index: int, text: str) -> bytes:
    """Replace the displayed text of text block `index`."""
    blocks = _blocks(payload)
    if not 0 <= index < len(blocks):
        raise ValueError(f"控件只有 {len(blocks)} 个文本块，没有第 {index} 个")
    old_text = blocks[index]["text"]
    if not old_text:
        raise ValueError("这个文本块是空的，暂不支持从空值写入")
    return replace_global(payload, old_text, text)


def set_text_font(payload: bytes, face: str, index: int | None = None) -> bytes:
    """Set the font face of one text block, or of all of them."""
    blocks = _blocks(payload)
    targets = blocks if index is None else [blocks[index]]
    if not targets:
        raise ValueError("控件没有文本块")

    out = payload
    for block in targets:
        if not block["font"]:
            continue
        out = replace_global(out, block["font"], face)
    return out


def action_window_offset(payload: bytes) -> int | None:
    """Offset of the window name a button action points at.

    A button saved in another project keeps its action, which refers to a window
    that may not exist here; MCGS then reports 未知窗口名. The reference is the
    first non-empty cstring after the control metadata block.
    """
    lay = walk_common(payload)
    for off, _end, text in _wide_strings(payload):
        if off > lay.after_metadata and text:
            return off
    return None


def set_action_window(payload: bytes, name: str = "") -> bytes:
    """Point the button action at 
ame; an empty name clears the reference."""
    offset = action_window_offset(payload)
    if offset is None:
        return payload
    return _replace_at(payload, offset, name)


# Templates that draw themselves from an embedded image. Setting fill or border
# on these writes the bytes but nothing changes on screen, because the image is
# painted over the control's own background.
IMAGE_BASED = {0x5009, 0x5025, 0x5027, 0x5028, 0x502A, 0x502D, 0x5031, 0x5032, 0x503D}


def is_image_based(type_id: int) -> bool:
    return type_id in IMAGE_BASED


# Button action modes, in the order MCGS stores them.
ACTION_MODES = {
    0: "\u7f6e1",        # 置1
    1: "\u7f6e0",        # 置0
    2: "\u53d6\u53cd",  # 取反
    3: "\u63091\u677e0",  # 按1松0
    4: "\u63090\u677e1",  # 按0松1
}
ACTION_MODE_INDEX = {v: k for k, v in ACTION_MODES.items()}

# 1.0f followed by 100.0f; the mode word sits immediately before it and the
# pattern occurs exactly once per payload.
_MODE_ANCHOR = struct.pack("<f", 1.0) + struct.pack("<f", 100.0)

# the flag word carries bit 2 for the press/release modes
PRESS_RELEASE_BIT = 0x0004


def action_mode(payload: bytes) -> str | None:
    """Which of the five button-action modes the control uses."""
    i = payload.find(_MODE_ANCHOR)
    if i < 4:
        return None
    value = struct.unpack_from("<I", payload, i - 4)[0]
    return ACTION_MODES.get(value)


def set_action_mode(payload: bytes, mode: str) -> bytes:
    """Set the button-action mode.

    Two fields move together: the mode word before the float pair, and bit 2 of
    the header flag word, which is set for the press/release modes.
    """
    index = ACTION_MODE_INDEX.get(mode)
    if index is None:
        raise ValueError(f"不认识的动作模式 {mode!r}，可用: {sorted(ACTION_MODE_INDEX)}")

    i = payload.find(_MODE_ANCHOR)
    if i < 4:
        raise FormatError("找不到动作模式字段")

    lay = walk_common(payload)
    out = bytearray(payload)
    struct.pack_into("<I", out, i - 4, index)

    flag = struct.unpack_from("<H", out, lay.flag_offset)[0]
    if index in (3, 4):
        flag |= PRESS_RELEASE_BIT
    else:
        flag &= ~PRESS_RELEASE_BIT
    struct.pack_into("<H", out, lay.flag_offset, flag)
    return bytes(out)


# ---------------------------------------------------------------- 数值输入框格式
# Layout of a numeric-input record, located relative to the unit string so that
# changing one string does not invalidate the others:
#
#   unit_start - 20   u32  1 = 固定小数, 0 = 自然小数
#   unit_end   +  0   u32  单位是否显示
#   unit_end   +  4   u32  (未解释，恒为 1)
#   unit_end   +  8   u32  最小整数位数
#   unit_end   + 12   u32  固定小数位数（自然小数时为 0xFFFFFFFF）
#   caption_end +  0  f32  最大值
#   caption_end +  4  f32  最小值
#
# The record stores the unit twice; the caption sits between the two copies, just
# before the font string.

NATURAL_DECIMALS_SENTINEL = 0xFFFFFFFF


def input_blocks(payload: bytes) -> list[dict]:
    """Offsets of every numeric-input format block.

    A block is a short wide string (the unit) followed by four words:

        unit cstring | unit_enabled | const | min_digits | decimals
                        +0            +4      +8           +12

    The unit and these words appear once or twice per record. An earlier version
    keyed on a 0xFFFFFFFF sentinel eight bytes before the unit, but MCGS clears
    that value when it normalises a bound control, so the shape itself is used
    instead: unit_enabled must be 0 or 1, min digits 0..8, and decimals either
    -1 or 0..8.
    """
    strings = _wide_strings(payload)
    if len(strings) < 4:
        raise FormatError(f"输入框字符串表只有 {len(strings)} 项，结构不符")

    def candidate(offset: int, end: int) -> dict | None:
        if end + 16 > len(payload):
            return None
        enabled = struct.unpack_from("<I", payload, end)[0]
        digits = struct.unpack_from("<I", payload, end + 8)[0]
        decimals = struct.unpack_from("<I", payload, end + 12)[0]
        if enabled not in (0, 1) or not 0 <= digits <= 8:
            return None
        if not (decimals == NATURAL_DECIMALS_SENTINEL or 0 <= decimals <= 8):
            return None
        return {
            "unit": (offset, end),
            # 标志位在单位串起点前 16 字节；end-12 指的是哨兵位置，写进去会毁掉哨兵
            "fixed_decimals_flag": offset - 16,
            "unit_enabled": end,
            "min_integer_digits": end + 8,
            "decimals": end + 12,
        }

    # 第一遍：12 字节前有 0xFFFFFFFF 的才是真块，精确且不会误判
    blocks: list[dict] = []
    for offset, end, text in strings:
        if len(text) > 8 or end - 12 < 0:
            continue
        # 哨兵在单位串起点前 8 字节。用 end-12 只在单位为空(4字节)时碰巧成立，
        # 单位非空时就会错位，导致设了单位之后再也找不到格式块。
        if offset < 8:
            continue
        if struct.unpack_from("<I", payload, offset - 8)[0] != NATURAL_DECIMALS_SENTINEL:
            continue
        found = candidate(offset, end)
        if found:
            blocks.append(found)

    # 第二遍：MCGS 规范化已绑定的控件时会清掉哨兵，此时退回按结构识别。
    # 紧跟在字体串后面的那个空串会碰巧符合形状，但它不是格式块，排除掉。
    if not blocks:
        font_indexes = {i for i, (_o, _e, t) in enumerate(strings)
                        if t in ("宋体", "微软雅黑", "黑体", "仿宋")}
        for idx, (offset, end, text) in enumerate(strings):
            if len(text) > 8 or (idx - 1) in font_indexes:
                continue
            found = candidate(offset, end)
            if found:
                blocks.append(found)

    if not blocks:
        raise FormatError("找不到输入框的格式块")

    # 文字锚点：字体前两个字符串，最值紧跟其后
    font_idx = next((idx for idx, (_o, _e, t) in enumerate(strings)
                     if t in ("宋体", "微软雅黑", "黑体", "仿宋")), None)
    if font_idx is None or font_idx < 2:
        raise FormatError("找不到字体串")
    anchor = strings[font_idx - 2]

    return blocks, {
        "caption": (anchor[0], anchor[1]),
        "maximum": anchor[1],
        "minimum": anchor[1] + 4,
    }


def input_layout(payload: bytes) -> dict:
    """First format block plus the caption-relative fields, flattened."""
    blocks, tail = input_blocks(payload)
    first = blocks[0]
    return {
        "fixed_decimals_flag": first["fixed_decimals_flag"],
        "unit_enabled": first["unit_enabled"],
        "min_integer_digits": first["min_integer_digits"],
        "decimals": first["decimals"],
        "unit": first["unit"],
        "caption": tail["caption"],
        "maximum": tail["maximum"],
        "minimum": tail["minimum"],
    }


def read_input_format(payload: bytes) -> dict:
    # MCGS reads the last copy; the first can be left stale
    blocks, _tail = input_blocks(payload)
    last = blocks[-1]
    lay = input_layout(payload)
    lay.update({k: last[k] for k in
                ("fixed_decimals_flag", "unit_enabled",
                 "min_integer_digits", "decimals", "unit")})
    decimals = struct.unpack_from("<I", payload, lay["decimals"])[0]
    # a set decimal count means natural decimals are off; MCGS keeps -1 otherwise
    natural = decimals == NATURAL_DECIMALS_SENTINEL
    return {
        "unit": Reader(payload, lay["unit"][0]).cstring(),
        "unit_enabled": struct.unpack_from("<I", payload, lay["unit_enabled"])[0] != 0,
        "natural_decimals": natural,
        "decimals": None if natural else decimals,
        "min_integer_digits": struct.unpack_from("<I", payload, lay["min_integer_digits"])[0],
        "maximum": struct.unpack_from("<f", payload, lay["maximum"])[0],
        "minimum": struct.unpack_from("<f", payload, lay["minimum"])[0],
    }


def _put_u32(payload: bytes, offset: int, value: int) -> bytes:
    out = bytearray(payload)
    struct.pack_into("<I", out, offset, value & 0xFFFFFFFF)
    return bytes(out)


def _put_f32(payload: bytes, offset: int, value: float) -> bytes:
    out = bytearray(payload)
    struct.pack_into("<f", out, offset, value)
    return bytes(out)


def set_input_format(payload: bytes, *, decimals: int | None = None,
                     natural_decimals: bool | None = None,
                     min_integer_digits: int | None = None,
                     unit: str | None = None,
                     unit_enabled: bool | None = None,
                     minimum: float | None = None,
                     maximum: float | None = None) -> bytes:
    """Rewrite the numeric-input format fields.

    Setting `decimals` turns natural decimals off automatically, because MCGS
    itself clears the flag when a fixed decimal count is in force; asking for both
    would be contradictory.
    """
    if decimals is not None and natural_decimals:
        raise ValueError("指定了小数位数就不能同时开自然小数")
    if decimals is not None and not 0 <= decimals <= 8:
        raise ValueError("小数位数必须在 0..8 之间")
    if min_integer_digits is not None and not 0 <= min_integer_digits <= 8:
        raise ValueError("最小整数位数必须在 0..8 之间")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("最小值不能大于最大值")

    # 先换单位文字（会移动后面的偏移），再重新定位
    if unit is not None:
        current = read_input_format(payload)["unit"]
        if current != unit:
            # both copies have to move together, and the unit sits inside the
            # metadata blob, so its length prefix has to follow
            blocks, _tail = input_blocks(payload)
            for block in reversed(blocks):
                payload = replace_cstring_at(payload, block["unit"][0], unit)
    lay = input_layout(payload)

    blocks, _tail = input_blocks(payload)
    for block in blocks:
        if natural_decimals is not None:
            payload = _put_u32(payload, block["fixed_decimals_flag"],
                               0 if natural_decimals else 1)
            if natural_decimals:
                payload = _put_u32(payload, block["decimals"],
                                   NATURAL_DECIMALS_SENTINEL)
        if decimals is not None:
            payload = _put_u32(payload, block["fixed_decimals_flag"], 1)
            payload = _put_u32(payload, block["decimals"], decimals)
        if min_integer_digits is not None:
            payload = _put_u32(payload, block["min_integer_digits"],
                               min_integer_digits)
        if unit_enabled is not None:
            payload = _put_u32(payload, block["unit_enabled"],
                               1 if unit_enabled else 0)
    if maximum is not None:
        payload = _put_f32(payload, lay["maximum"], maximum)
    if minimum is not None:
        payload = _put_f32(payload, lay["minimum"], minimum)
    return payload


def make_control(kind: str, name: str, rect, *,
                 fill: str | None = None,
                 border_color: str | None = None,
                 border_width: int | None = None,
                 text_off: str | None = None,
                 text_on: str | None = None,
                 text_font: str | None = None,
                 action_window: str | None = None) -> tuple[bytes, int]:
    """Place any templated control with the given geometry.

    Returns (payload, type_id). Geometry, fill, border and the on/off text of
    switches and lamps are rewritten; everything else keeps the template's own
    defaults, because the remaining internal fields have not been mapped.
    """
    type_id = KIND_NAMES.get(kind)
    if type_id is None:
        raise ValueError(f"不认识的控件类型 {kind!r}，可用: {sorted(KIND_NAMES)}")

    payload = template(type_id)
    payload = set_name(payload, name)
    payload = set_rect(payload, rect)
    if type_id == TYPE_BUTTON:
        # the template came from another project; its action window does not
        # exist here and MCGS flags it as 未知窗口名
        payload = set_action_window(payload, action_window or "")
    if fill is not None:
        payload = set_fill(payload, _fill_value(fill))
    if border_color is not None or border_width is not None:
        payload = set_border(payload, border_color or "#000000", border_width)

    if text_off is not None or text_on is not None:
        if not _blocks(payload):
            raise ValueError(f"{kind} 没有可改的文本块")
        if text_off is not None:
            payload = set_text(payload, 0, text_off)
        if text_on is not None:
            payload = set_text(payload, 1, text_on)
    if text_font is not None and _blocks(payload):
        payload = set_text_font(payload, text_font)

    return payload, type_id


# ------------------------------------------------------------------ stream
def write_count(n: int) -> bytes:
    return (struct.pack("<H", n) if n < 0xFFFF
            else struct.pack("<H", 0xFFFF) + struct.pack("<I", n))


def embedded_tags(payload: bytes) -> list[str]:
    """MFC class tags registered inside a payload, in the order they are read.

    Non-text controls carry child objects (``CDrawCell``) that register their own
    class, which consumes a registry slot. Callers that reference classes by
    index have to account for these.
    """
    names = []
    i = 0
    while True:
        i = payload.find(b"\xff\xff\x00\x00", i)
        if i < 0:
            break
        pos = i + 4
        if pos + 2 > len(payload):
            break
        n = struct.unpack_from("<H", payload, pos)[0]
        pos += 2
        raw = payload[pos:pos + n]
        if 0 < n <= 64 and raw.isascii() and raw[:1].isalpha():
            names.append(raw.decode("ascii"))
        i += 1
    return names


def build_stream(payloads: list[bytes], type_ids: list[int] | None = None) -> bytes:
    """Wrap payloads in the MFC archive form MCGS expects on the clipboard.

    Every control gets a full class tag rather than a ``0x8000|slot`` reference.
    Slot arithmetic is not safe here: a payload registers classes of its own
    (``CDrawCell`` for switches, lamps and inputs, ``CAniOutput`` for bound
    outputs), so a reference can point at the wrong class and MCGS then either
    rejects the stream or builds the wrong object. Re-registering a class is
    accepted — verified for ``CAniOutput`` and for whole mixed pages.
    """
    if type_ids is None:
        type_ids = [TYPE_LABEL] * len(payloads)
    if len(type_ids) != len(payloads):
        raise ValueError("type_ids 与 payloads 数量不一致")

    out = bytearray(write_count(len(payloads)))
    for payload, type_id in zip(payloads, type_ids):
        class_name = TYPE_CLASS.get(type_id)
        if class_name is None:
            raise ValueError(f"未知控件类型 0x{type_id:04X}，不知道剪贴板类名")
        raw = class_name.encode("ascii")
        out += struct.pack("<HHH", 0xFFFF, 0, len(raw)) + raw
        out += payload
    return bytes(out)

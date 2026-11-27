"""Read and write the PLC channel a control is bound to.

Every control that shows or drives a PLC value stores the channel as a wide
string shaped like ``<device>_<channel>`` — for example ``设备0_读写DWUB1000``.
The device name comes from the project's device table, so a binding can be found
by string shape instead of by a per-type offset, which differs between control
types and even between two shapes of the same type.

Where the slot sits when a control has no binding yet:

    0x5025 数值输入   字体串前两个
    0x502A 指示灯     倒数第二个
    0x5028 开关       最后一个字体串之后，连续两个

The switch stores it twice and MCGS reads the second copy, so both are written.
"""

from __future__ import annotations

from .controls import FormatError, Reader, _wide_strings, wide_cstring

# 每种类型未绑定时变量槽的规则
UNBOUND_SLOT = {
    0x5025: "font_minus_2",
    0x502A: "index:-2",
    0x5028: "after_last_font",
}

FONT_FACES = ("宋体", "微软雅黑", "黑体", "仿宋", "楷体", "Arial")


def device_prefixes(device_names: list[str]) -> list[str]:
    """``设备0_``-style prefixes for the devices configured in the project."""
    return [f"{name}_" for name in device_names if name]


def _trim(text: str) -> str:
    """Cut the string at the first control character.

    Some controls (0x5031 percentage fill) store the channel name and binary
    parameters in one field whose length covers both, so the decoded text runs on
    into data that is not part of the name. Real channel names never contain
    control characters.
    """
    for i, ch in enumerate(text):
        if ord(ch) < 0x20 and ch not in "\r\n\t":
            return text[:i]
    return text


def find_bindings(payload: bytes, prefixes: list[str]) -> list[tuple[int, str]]:
    """Every (offset, text) whose text starts with one of the device prefixes."""
    hits = []
    for offset, _end, text in _wide_strings(payload):
        trimmed = _trim(text)
        if any(trimmed.startswith(p) for p in prefixes):
            hits.append((offset, trimmed))
    return hits


def read_binding(payload: bytes, prefixes: list[str]) -> str | None:
    """The channel this control is bound to, or None."""
    hits = find_bindings(payload, prefixes)
    if not hits:
        return None
    return hits[0][1]


def read_max_binding(payload: bytes, prefixes: list[str]) -> str | None:
    """The upper-limit channel of a numeric input, or None.

    A 数值输入 stores two channel names back to back: the value first, then the
    upper limit. Other control types only ever have one.
    """
    hits = find_bindings(payload, prefixes)
    if len(hits) < 2:
        return None
    return hits[1][1]


def write_max_binding(payload: bytes, prefixes: list[str], variable: str,
                      type_id: int) -> bytes:
    """Point the numeric input's upper limit at `variable`, or clear it."""
    if type_id != 0x5025:
        raise FormatError(
            f"0x{type_id:04X} 没有最大值变量槽，只有数值输入(0x5025)有。")

    current = read_max_binding(payload, prefixes)
    if current == variable:
        return payload
    if current is None:
        if variable == "":
            return payload
        raise FormatError(
            "这个输入框没有最大值变量槽，无法从无到有添加。"
            "请先在 MCGS 的属性框里设一次最大值变量，之后再改。")
    return _replace_all(payload, current, variable)


def _replace_all(payload: bytes, old: str, new: str) -> bytes:
    """Replace every copy of a channel name, keeping the length fields correct."""
    from .builder import replace_global
    return replace_global(payload, old, new, limit=8)


def _replace_one(payload: bytes, offset: int, new_text: str) -> bytes:
    """Replace the wide string at `offset`, leaving everything else untouched."""
    reader = Reader(payload, offset)
    reader.cstring()
    consumed = reader.pos - offset
    return payload[:offset] + wide_cstring(new_text) + payload[offset + consumed:]


def _replace_prefix(payload: bytes, offset: int, old_name: str,
                    new_name: str) -> bytes | None:
    """Swap the leading name of a field that carries more than the name.

    Returns None when the field does not actually start with `old_name`.
    """
    reader = Reader(payload, offset)
    full = reader.cstring()
    consumed = reader.pos - offset
    if not full.startswith(old_name):
        return None
    rebuilt = new_name + full[len(old_name):]
    return payload[:offset] + wide_cstring(rebuilt) + payload[offset + consumed:]


def _slot_offset(payload: bytes, type_id: int, index: int | None) -> int:
    """Byte offset of the variable slot when the control has no binding yet."""
    strings = _wide_strings(payload)
    rule = UNBOUND_SLOT.get(type_id)
    if rule is None:
        raise FormatError(f"0x{type_id:04X} 类型暂不支持从无到有绑定变量")

    if rule.startswith("index:"):
        want = int(rule.split(":", 1)[1])
        pos = want if want >= 0 else len(strings) + want
        if not 0 <= pos < len(strings):
            raise FormatError(f"字符串表只有 {len(strings)} 项，取不到第 {want} 项")
        return strings[pos][0]

    if rule == "font_minus_2":
        font_positions = [i for i, (_o, _e, t) in enumerate(strings) if t in FONT_FACES]
        if not font_positions:
            raise FormatError("输入框里找不到字体串，无法定位变量槽")
        pos = font_positions[0] - 2
        if not 0 <= pos < len(strings):
            raise FormatError(f"字体在字符串表第 {font_positions[0]} 项，前两项不存在")
        return strings[pos][0]

    if rule == "after_last_font":
        font_positions = [i for i, (_o, _e, t) in enumerate(strings) if t in FONT_FACES]
        if not font_positions:
            raise FormatError("开关里找不到字体串，无法定位变量槽")
        nxt = font_positions[-1] + 1
        if nxt >= len(strings):
            raise FormatError("变量槽超出字符串表")
        return strings[nxt][0]

    raise FormatError(f"未知的槽规则 {rule}")


BUTTON_TYPE = 0x5027


def _looks_like_script(text: str) -> bool:
    return "=" in text or "\n" in text


def write_binding(payload: bytes, prefixes: list[str], variable: str,
                  type_id: int) -> bytes:
    """Point the control at `variable`, or clear its binding when empty.

    An already-bound control has every copy rewritten. An unbound one gets the
    name written into its slot; the switch needs two consecutive slots, which is
    handled by writing the second slot too.

    A button stores an action script rather than a single channel, so a bare
    channel name is rejected for buttons — assigning one would overwrite the
    script.
    """
    if type_id == BUTTON_TYPE and variable and not _looks_like_script(variable):
        raise FormatError(
            "按钮存的是动作脚本，不是单个通道名。"
            "整体替换脚本请传入完整内容（含 = 或换行）；"
            "若只想改某个通道，请先在 MCGS 里确认脚本结构。")

    if type_id == BUTTON_TYPE and variable == "":
        raise FormatError(
            "按钮的动作是一段脚本，清空它会删除整段动作。"
            "如需去掉动作，请在 MCGS 的属性框里操作。")

    current = read_binding(payload, prefixes)

    if variable == "":
        if current is None:
            return payload
        return _replace_all(payload, current, "")

    if current is not None:
        if current == variable:
            return payload
        try:
            result = _replace_all(payload, current, variable)
        except FormatError:
            # walk_common 读不通。只有在完全没有动画块时才敢按偏移直接替换：
            # 动画块里也有长度字段，绕过它会让 MCGS 保存时丢掉整个控件。
            if b"CAni" in payload:
                raise FormatError(
                    "这个控件带有解析不了的动画块，改绑会破坏它的内部长度。"
                    "请在 MCGS 属性框里手动改。") from None
            hits = find_bindings(payload, prefixes)
            if len(hits) != 1:
                raise
            result = _replace_one(payload, hits[0][0], variable)
        if result == payload:
            # 某些控件（如 0x5031 百分比填充）把变量名和二进制参数存在同一个
            # 字段里，长度前缀覆盖两者，标准宽字符串匹配不到。名字是那个字段
            # 的前缀，可以整体重建，参数部分原样保留。
            hits = find_bindings(payload, prefixes)
            if len(hits) == 1:
                rebuilt = _replace_prefix(payload, hits[0][0], current, variable)
                if rebuilt is not None:
                    return rebuilt
            raise FormatError(
                f"改绑没有生效：{current!r} 在这个控件里不是标准宽字符串，"
                "可能该类型把变量和参数存在同一个字段。请在 MCGS 属性框里手动改。")
        return result

    # 未绑定：把名字写进槽位
    offset = _slot_offset(payload, type_id, None)
    reader = Reader(payload, offset)
    reader.cstring()
    consumed = reader.pos - offset
    new = wide_cstring(variable)
    delta = len(new) - consumed

    out = payload[:offset] + new + payload[offset + consumed:]
    if delta:
        from .builder import _patch_count, _patch_nested_lengths, walk_common
        lay = walk_common(payload)
        meta_start = lay.after_metadata - lay.metadata_len
        if meta_start <= offset < lay.after_metadata:
            out = _patch_count(out, lay.metadata_count, lay.metadata_len + delta)
            out = _patch_nested_lengths(out, lay, delta)

    # 开关有两份：紧随其后的那个槽也要写
    if type_id == 0x5028:
        strings = _wide_strings(out)
        first = next((i for i, (_o, _e, t) in enumerate(strings) if t == variable), None)
        if first is not None and first + 1 < len(strings):
            second = strings[first + 1][0]
            r2 = Reader(out, second)
            r2.cstring()
            consumed2 = r2.pos - second
            out = out[:second] + wide_cstring(variable) + out[second + consumed2:]

    return out

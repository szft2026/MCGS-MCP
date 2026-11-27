"""Add, modify and delete text labels on a user window.

MCGS cannot append to a canvas, so every change is applied by rewriting the whole
page: the existing controls are read back, the requested change is applied to
that list, and the list is pasted back in one go. Existing controls keep their
original bytes, so fonts, animations and bindings survive untouched.
"""

from __future__ import annotations

from pathlib import Path

from . import binding, builder, controls, device, page, project as project_mod, win32

LABEL_TYPE = 0x5008


def _load(window: str, project: str | None = None) -> tuple[str, list[dict]]:
    """Current controls of `window`, as raw records."""
    try:
        _pid, path = win32.resolve_project(project)
    except win32.ProjectLookupError as exc:
        raise page.PageError(exc.code, exc.message, available=exc.available) from None

    info = project_mod.read_project(path)
    target = next((w for w in info.windows if w.name == window), None)
    if target is None:
        raise page.PageError(
            "WINDOW_NOT_FOUND", f"没有名为 {window!r} 的窗口。",
            available=[w.name for w in info.windows])
    return path, target.controls


# 设备表解析一次约 1 秒，而它只在工程文件变化时才变
_BINDING_CACHE: dict[tuple[str, float], tuple[list[str], set[str]]] = {}


def _binding_context(project_path: str) -> tuple[list[str], set[str]]:
    """Device prefixes and the full set of channel names in the project."""
    try:
        stamp = Path(project_path).stat().st_mtime
    except OSError:
        return [], set()

    key = (str(project_path), stamp)
    cached = _BINDING_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        data = device.read_devices(project_path)
    except Exception:
        return [], set()

    names, channels = [], set()
    for window in data.get("device_windows", []):
        for dev in window.get("devices", []):
            names.append(dev["name"])
            for ch in dev.get("channels", []):
                channels.add(ch["channel"])

    result = (binding.device_prefixes(names), channels)
    _BINDING_CACHE.clear()          # 只保留最新一份，避免无限增长
    _BINDING_CACHE[key] = result
    return result


def _payloads(records: list[dict]) -> tuple[list[bytes], list[int]]:
    return [r["payload"] for r in records], [r["type_id"] for r in records]



def _image_warning(type_id: int, *requested) -> list[str]:
    """Warn when appearance was set on a control whose look comes from an image."""
    if not builder.is_image_based(type_id):
        return []
    if not any(v is not None for v in requested):
        return []
    return [f"0x{type_id:04X} 的外观由内嵌图片绘制，填充色/边框写进去了但界面上不会变化。"]


def _summary(result: dict, warnings: list[str] | None = None) -> dict:
    """Trim the page result down to what a caller needs."""
    keep = ("index", "kind", "name", "caption", "rect", "size", "fill",
            "border", "text_color", "font", "texts", "text_off", "text_on",
            "text_faces", "unit", "action_variable", "action_mode", "number_format",
            "variable", "max_variable")
    return {
        "ok": result["ok"],
        "window": result["window"],
        "control_count": result["control_count"],
        "controls": [{k: c[k] for k in keep if k in c} for c in result["controls"]],
        "check_errors": result["check_errors"],
        "check_passed": result.get("check_passed", not result["check_errors"]),
        "backup": result["backup"],
        "dialogs_closed": result.get("dialogs_closed") or [],
        "warnings": warnings or [],
        **({"check_warning": result["warning"]} if result.get("warning") else {}),
    }


def add_label(window: str, caption: str, x: int, y: int,
              width: int, height: int, *,
              font_size: int | None = None,
              font_face: str | None = None,
              font_weight: int | None = None,
              fill: str | None = None,
              text_color: str | None = None,
              border_color: str | None = None,
              border_width: int | None = None,
              name: str | None = None,
              project: str | None = None) -> dict:
    """Append one label to `window` and save."""
    if width <= 0 or height <= 0:
        raise page.PageError("BAD_RECT", "框的宽高必须大于 0。")

    _, records = _load(window, project)

    rect = (int(x), int(y), int(x) + int(width), int(y) + int(height))
    label = builder.make_label(
        name or f"标签{len(records) + 1}",
        caption,
        rect,
        size_px=font_size,
        face=font_face,
        weight=font_weight,
        text_color=text_color,
        fill=fill,
    )
    if border_color is not None or border_width is not None:
        label = builder.set_border(label, border_color or "#000000", border_width)

    payloads, type_ids = _payloads(records)
    payloads.append(label)
    type_ids.append(LABEL_TYPE)

    return _summary(page.replace_page(window, payloads, type_ids, project=project))


UPDATABLE = ("caption", "x", "y", "width", "height", "font_size", "font_face",
             "font_weight", "fill", "text_color", "border_color", "border_width",
             "text_off", "text_on", "text_font", "action_variable", "action_mode",
             "decimals", "natural_decimals", "min_integer_digits", "unit",
             "unit_enabled", "minimum", "maximum", "variable", "max_variable")


def _apply_to_payload(records: list[dict], index: int, fields: dict,
                      binding_ctx: tuple[list[str], set[str]] | None = None
                      ) -> tuple[bytes, list[str]]:
    """Validate and apply one control's field changes, returning the new payload.

    Split out of `update_control` so a batch can reuse it without rebuilding the
    page once per control.
    """
    if not 0 <= index < len(records):
        raise page.PageError(
            "INDEX_OUT_OF_RANGE",
            f"序号 {index} 超出范围，该窗口有 {len(records)} 个控件。")

    record = records[index]
    type_id = record["type_id"]
    payload = record["payload"]
    current = controls.parse_control(index, type_id, payload)
    # 解析失败只影响需要读结构的字段（目前只有按钮动作）；变量和数值格式
    # 直接改 payload 的字符串/字节，不依赖完整解析，不该被它挡住。
    if current.error and fields.get("action_variable") is not None:
        raise page.PageError("PARSE_FAILED", f"序号 {index} 解析失败: {current.error}")

    get = lambda k: fields.get(k)
    warnings: list[str] = []
    if current.error:
        warnings.append(
            f"序号 {index} 的结构没能完整解析（{current.error}）；"
            "本次只改了不依赖该结构的部分，其他属性请在 MCGS 里核对。")

    label_only = (get("caption"), get("font_size"), get("font_face"),
                  get("font_weight"), get("text_color"))
    if any(v is not None for v in label_only) and type_id != LABEL_TYPE:
        raise page.PageError(
            "UNSUPPORTED_CONTROL",
            f"序号 {index} 是 0x{type_id:04X} 类型，文字/字体/字色只能改文字标签；"
            "开关和指示灯用 text_off / text_on / text_font。")

    _prefixes, _valid = binding_ctx or ([], set())
    was_unbound = (get("variable") is not None
                   and not binding.read_binding(payload, _prefixes))

    if get("variable") is not None:
        prefixes, valid = binding_ctx or ([], set())
        if not prefixes:
            raise page.PageError(
                "NO_DEVICE",
                "工程里没有配置 PLC 设备，无法绑定变量。")
        wanted = get("variable")
        # 按钮存的是动作脚本而不是通道名，不能按通道表校验
        if wanted and type_id != 0x5027 and wanted not in valid:
            raise page.PageError(
                "UNKNOWN_VARIABLE",
                f"设备通道表里没有 {wanted!r}。可用通道见 mcgs_get_device。",
                available=sorted(valid)[:40])
        try:
            payload = binding.write_binding(payload, prefixes, wanted, type_id)
        except controls.FormatError as exc:
            raise page.PageError("UNSUPPORTED_CONTROL", str(exc)) from None

    number_format = (get("decimals"), get("natural_decimals"),
                     get("min_integer_digits"), get("unit"),
                     get("unit_enabled"), get("minimum"), get("maximum"))
    if any(v is not None for v in number_format):
        if type_id != 0x5025:
            raise page.PageError(
                "UNSUPPORTED_CONTROL",
                f"序号 {index} 是 0x{type_id:04X}，只有输入框(0x5025)有数值格式设置")
        try:
            payload = builder.set_input_format(
                payload, decimals=get("decimals"),
                natural_decimals=get("natural_decimals"),
                min_integer_digits=get("min_integer_digits"),
                unit=get("unit"), unit_enabled=get("unit_enabled"),
                minimum=get("minimum"), maximum=get("maximum"))
        except (ValueError, controls.FormatError) as exc:
            raise page.PageError("BAD_ARGUMENT", str(exc)) from None
        if was_unbound:
            warnings.append(
                "首次绑定变量时 MCGS 会把数值格式重置为默认值；"
                "本次设置的小数位/单位/范围可能被覆盖，保存后请读回确认，"
                "必要时再调用一次设置格式。")

    if get("max_variable") is not None:
        if type_id != 0x5025:
            raise page.PageError(
                "UNSUPPORTED_CONTROL",
                f"序号 {index} 是 {kind}，只有数值输入有最大值变量槽。")
        try:
            payload = binding.write_max_binding(
                payload, _prefixes, get("max_variable"), type_id)
        except (ValueError, controls.FormatError) as exc:
            raise page.PageError("BAD_ARGUMENT", str(exc)) from None

    if get("action_mode") is not None:
        try:
            payload = builder.set_action_mode(payload, get("action_mode"))
        except (ValueError, controls.FormatError) as exc:
            raise page.PageError("BAD_ARGUMENT", str(exc)) from None

    if get("action_variable") is not None:
        if not current.action_variable:
            raise page.PageError(
                "UNSUPPORTED_CONTROL",
                f"序号 {index} 没有按钮动作，暂不支持添加动作；"
                "只能改已有动作的目标变量。请在 MCGS 里先给按钮加一个动作。")
        try:
            payload = controls.set_action_variable(payload, get("action_variable"))
        except controls.FormatError as exc:
            raise page.PageError("UNSUPPORTED_CONTROL", str(exc)) from None

    text_only = (get("text_off"), get("text_on"), get("text_font"))
    if any(v is not None for v in text_only):
        if type_id == LABEL_TYPE:
            raise page.PageError(
                "UNSUPPORTED_CONTROL",
                f"序号 {index} 是文字标签，改文字请用 caption，不是 text_off/text_on。")
        if not current.texts:
            raise page.PageError(
                "UNSUPPORTED_CONTROL",
                f"序号 {index} 是 0x{type_id:04X} 类型，没有可改的开关/指示灯文本块。")

    x1, y1, x2, y2 = current.rect
    if get("x") is not None:
        x2 = get("x") + (x2 - x1)
        x1 = get("x")
    if get("y") is not None:
        y2 = get("y") + (y2 - y1)
        y1 = get("y")
    if get("width") is not None:
        x2 = x1 + int(get("width"))
    if get("height") is not None:
        y2 = y1 + int(get("height"))
    rect = (x1, y1, x2, y2)
    if rect[0] >= rect[2] or rect[1] >= rect[3]:
        raise page.PageError("BAD_RECT", f"矩形不合法: {rect}")

    # 只有真的要改位置/尺寸时才写矩形；否则像 CAniBrush 这类
    # walk_common 读不通的控件连改个变量都会被它挡住。
    if any(get(k) is not None for k in ("x", "y", "width", "height")):
        payload = builder.set_rect(payload, rect)
    if get("caption") is not None:
        payload = builder.set_caption(payload, get("caption"))
    if any(get(k) is not None for k in ("font_size", "font_face", "font_weight")):
        payload = builder.set_font(payload, get("font_size"), get("font_face"),
                                   get("font_weight"))
    if get("fill") is not None:
        payload = builder.set_fill(payload, None if get("fill") == "none" else get("fill"))
    if get("text_color") is not None:
        payload = builder.set_text_color(payload, get("text_color"))
    if get("border_color") is not None or get("border_width") is not None:
        payload = builder.set_border(payload, get("border_color") or "#000000",
                                     get("border_width"))
    if get("text_off") is not None:
        payload = builder.set_text(payload, 0, get("text_off"))
    if get("text_on") is not None:
        payload = builder.set_text(payload, 1, get("text_on"))
    if get("text_font") is not None:
        payload = builder.set_text_font(payload, get("text_font"))

    warnings = _image_warning(type_id, get("fill"), get("border_color"),
                              get("border_width"))
    return payload, warnings


def update_control(window: str, index: int, *, project: str | None = None,
                   **fields) -> dict:
    """Change one control in `window`, identified by its index.

    Geometry, fill and border apply to every type. Text and font fields are
    type-specific: caption / font_* / text_color for labels, and
    text_off / text_on / text_font for switches and lamps.
    """
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise page.PageError("BAD_ARGUMENT", f"不认识的参数: {sorted(unknown)}")

    path, records = _load(window, project)
    ctx = _binding_context(path)
    payload, warnings = _apply_to_payload(records, index, fields, ctx)

    payloads, type_ids = _payloads(records)
    payloads[index] = payload
    return _summary(page.replace_page(window, payloads, type_ids, project=project), warnings)


def update_many(window: str, updates: list[dict], *, project: str | None = None) -> dict:
    """Apply several control changes in one page rebuild.

    A page rewrite pushes the whole window through the clipboard, which costs
    seconds and grows with the window. Changing twenty controls one call at a
    time would rebuild twenty times; this rebuilds once.
    """
    if not isinstance(updates, list) or not updates:
        raise page.PageError("BAD_ARGUMENT", "updates 必须是非空数组")

    path, records = _load(window, project)
    ctx = _binding_context(path)
    payloads, type_ids = _payloads(records)

    applied = []
    warnings: list[str] = []
    for item in updates:
        if not isinstance(item, dict) or "index" not in item:
            raise page.PageError("BAD_ARGUMENT", "每个 update 必须带 index")
        index = item["index"]
        fields = {k: v for k, v in item.items() if k != "index"}
        unknown = set(fields) - set(UPDATABLE)
        if unknown:
            raise page.PageError("BAD_ARGUMENT",
                                 f"序号 {index} 有不认识的参数: {sorted(unknown)}")
        payload, warn = _apply_to_payload(records, index, fields, ctx)
        payloads[index] = payload
        applied.append(index)
        warnings.extend(warn)

    result = _summary(page.replace_page(window, payloads, type_ids, project=project),
                      sorted(set(warnings)))
    # 批量改几个控件，不该把整页控件列表吐回去
    touched = set(applied)
    result["controls"] = [c for c in result["controls"] if c["index"] in touched]
    result["updated"] = applied
    return result


def add_control(window: str, kind: str, x: int, y: int,
                width: int, height: int, *,
                name: str | None = None,
                fill: str | None = None,
                border_color: str | None = None,
                border_width: int | None = None,
                text_off: str | None = None,
                text_on: str | None = None,
                text_font: str | None = None,
                action_window: str | None = None,
                project: str | None = None) -> dict:
    """Append a non-text control (switch, lamp, input) to `window`.

    Geometry, fill, border and the on/off text of switches and lamps are
    rewritten; the remaining internal fields keep the template's defaults.
    """
    if width <= 0 or height <= 0:
        raise page.PageError("BAD_RECT", "框的宽高必须大于 0。")

    _, records = _load(window, project)

    rect = (int(x), int(y), int(x) + int(width), int(y) + int(height))
    payload, type_id = builder.make_control(
        kind, name or f"{kind}{len(records) + 1}", rect,
        fill=fill, border_color=border_color, border_width=border_width,
        text_off=text_off, text_on=text_on, text_font=text_font,
        action_window=action_window)

    payloads, type_ids = _payloads(records)
    payloads.append(payload)
    type_ids.append(type_id)

    warnings = _image_warning(type_id, fill, border_color, border_width)
    return _summary(page.replace_page(window, payloads, type_ids, project=project), warnings)


def delete_control(window: str, index: int, *,
                   project: str | None = None) -> dict:
    """Remove one label from `window`, identified by its index."""
    _, records = _load(window, project)

    if not 0 <= index < len(records):
        raise page.PageError(
            "INDEX_OUT_OF_RANGE",
            f"序号 {index} 超出范围，该窗口有 {len(records)} 个控件。")

    payloads, type_ids = _payloads(records)
    del payloads[index]
    del type_ids[index]

    return _summary(page.replace_page(window, payloads, type_ids, project=project))

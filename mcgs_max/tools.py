"""Tool implementations. Each function returns a plain JSON-serialisable dict."""

from __future__ import annotations

from . import device as device_mod
from . import labels as labels_mod
from . import page
from . import project as project_mod
from . import win32
from . import window as window_mod


def _guarded(fn):
    """Turn a PageError into a structured tool result instead of a crash."""
    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except page.PageError as exc:
            payload = {"ok": False, "error_code": exc.code, "message": exc.message}
            payload.update(exc.extra)
            return payload
        except ValueError as exc:
            return {"ok": False, "error_code": "BAD_ARGUMENT", "message": str(exc)}
        except OSError as exc:
            return {"ok": False, "error_code": "WINDOW_DRIVE_FAILED",
                    "message": f"{type(exc).__name__}: {exc}"}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _process_summary():
    procs = win32.list_processes()
    return procs


def get_project() -> dict:
    """Report what MCGS currently has open.

    Everything is discovered from the running process; no path is assumed.
    """
    processes = _process_summary()
    if not processes:
        return {
            "ok": False,
            "error_code": "NO_MCGS_PROCESS",
            "message": "没有正在运行的 McgsSetPro.exe。请先打开 MCGS 组态软件并载入工程。",
            "processes": [],
        }

    results = []
    for proc in processes:
        pid = proc["pid"]
        frame = win32.main_frame(pid)
        if frame is None:
            results.append({
                "pid": pid,
                "frame_found": False,
                "projects": [],
                "canvas": None,
                "dialogs": win32.modal_dialogs(pid),
            })
            continue

        paths = win32.workbench_projects(pid)
        canvas = win32.canvas_window(pid)
        entry = {
            "pid": pid,
            "frame_found": True,
            "frame_title": frame.title,
            "projects": [],
            "canvas": None,
            "dialogs": win32.modal_dialogs(pid),
        }

        if canvas:
            entry["canvas"] = {"hwnd": canvas.hwnd, "title": canvas.title}

        for item in paths:
            info = project_mod.read_project(item["path"])
            record = {
                "path": info.path,
                "name": info.name,
                "dirty": item["dirty"],
                "exists": info.exists,
                "size": info.size,
                "modified": info.modified,
                "readable": info.readable,
                "control_count": info.control_count,
                "windows": [
                    {"name": w.name, "control_count": w.control_count,
                     "blob_bytes": w.blob_bytes}
                    for w in info.windows
                ],
            }
            if info.error:
                record["error"] = info.error
            entry["projects"].append(record)

        if not paths:
            entry["note"] = (
                "找到 MCGS 进程，但没有探测到工程窗口标题。"
                "请确认已打开工程（工作台窗口标题应为「工作台 : <工程路径>」）。"
            )
        if entry["dialogs"]:
            entry["warning"] = (
                "存在模态对话框，MCGS 当前可能处于错误状态；"
                "在做任何写操作前应先处理这些对话框。"
            )
        results.append(entry)

    ok = any(r.get("projects") for r in results)
    payload = {"ok": ok, "process_count": len(processes), "instances": results}
    if not ok:
        payload["error_code"] = "NO_PROJECT_OPEN"
        payload["message"] = "MCGS 在运行，但未找到已打开的工程。"
    return payload


def get_device(project: str) -> dict:
    """Devices configured in the project and their PLC channel tables."""
    try:
        pid, path = win32.resolve_project(project)
    except win32.ProjectLookupError as exc:
        return {"ok": False, "error_code": exc.code, "message": exc.message,
                "available": exc.available}
    try:
        return device_mod.read_devices(path)
    except Exception as exc:
        return {"ok": False, "error_code": "DEVICE_READ_FAILED",
                "message": f"{type(exc).__name__}: {exc}"}


TOOLS = {
    "mcgs_get_project": {
        "fn": get_project,
        "description": (
            '列出 MCGS 当前打开的工程：文件路径、大小、修改时间、各窗口控件数、画布是否打开、有无阻塞对话框。只读。\nMCGS 未运行时返回 NO_MCGS_PROCESS；在运行但没有工程时返回 NO_PROJECT_OPEN。'
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "mcgs_get_device": {
        "fn": get_device,
        "description": (
            '读取工程的 PLC 设备与通道表。只读。\n每个通道返回三项：object（通道名称/PLC 地址）、channel（连接变量完整名）、register（寄存器类型）。绑定变量时直接用 channel，不要自己拼。\n没配设备时 devices 为空。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": (
                        "要读取的工程。必填。可以是完整路径、工程名，"
                        "或能唯一匹配的片段。"
                    ),
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    "mcgs_get_window": {
        "fn": window_mod.get_window,
        "description": (
            '读取一个用户窗口的全部控件。只读。\n每个控件返回 index、kind、rect、显示文字、填充色、字号字体；已绑变量的还返回 variable 和 max_variable；输入框还返回 number_format（小数位、单位、上下限）。\nindex 是控件在本页的位置，改删控件时用它定位。窗口不存在时返回 WINDOW_NOT_FOUND 并列出可用窗口名。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string",
                           "description": "窗口名，例如「窗口0」。必填。"},
                "project": {
                    "type": "string",
                    "description": (
                        "要操作的工程。必填。可以是完整路径、工程名，"
                        "或能唯一匹配的片段。"
                    ),
                },
            },
            "required": ["window", "project"],
            "additionalProperties": False,
        },
    },
    "mcgs_label_add": {
        "fn": _guarded(labels_mod.add_label),
        "description": (
            '在窗口里添加一个文字标签并保存。\n必填：window、caption、x、y、width、height、project。\n可选：font_size、font_face、font_weight、fill、text_color、border_color、border_width、name。fill 传 "none" 表示无填充。\nMCGS 不能向已有画布追加，所以会读出整页、追加、再重建；已有控件原样保留。写操作，约 10-15 秒。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "目标窗口名，见 mcgs_get_project"},
                "caption": {"type": "string", "description": "标签显示的文字"},
                "x": {"type": "integer", "description": "左上角 X 坐标"},
                "y": {"type": "integer", "description": "左上角 Y 坐标"},
                "width": {"type": "integer", "description": "框宽度（像素）"},
                "height": {"type": "integer", "description": "框高度（像素）"},
                "font_size": {"type": "integer", "description": "字号（像素高），可选"},
                "font_face": {"type": "string", "description": "字体名，可选，默认宋体"},
                "font_weight": {"type": "integer",
                                "description": "字重，400 常规 / 700 粗体，可选"},
                "fill": {"type": "string",
                         "description": "填充色 #RRGGBB，或 \"none\" 表示无填充，可选"},
                "text_color": {"type": "string", "description": "文字颜色 #RRGGBB，可选"},
                "border_color": {"type": "string", "description": "边框颜色 #RRGGBB，可选"},
                "border_width": {"type": "integer", "description": "边框线宽，可选"},
                "name": {"type": "string", "description": "控件名，可选"},
                "project": {
                    "type": "string",
                    "description": (
                        "要操作的工程。必填。可以是完整路径、工程名，"
                        "或能唯一匹配的片段。"
                    ),
                },
            },
            "required": ["window", "caption", "x", "y", "width", "height", "project"],
            "additionalProperties": False,
        },
    },
    "mcgs_control_update": {
        "fn": _guarded(labels_mod.update_control),
        "description": (
            '修改窗口里的一个控件并保存。任意类型都能改。\n必填：window、index、project。其余字段只传要改的。\n位置尺寸：x、y、width、height（只给 x 或 y 是平移，只给 width/height 是缩放）。\n外观：fill、border_color、border_width、text_color、font_size、font_face、font_weight。除文字标签外，其余控件的外观由内嵌图片绘制，改颜色不会生效（返回值 warnings 会提示）。\n文字：caption 只用于文字标签(0x5008)；开关(0x5028)和指示灯(0x502A)用 text_off / text_on / text_font。\n变量：variable 绑通道；输入框另有 max_variable 绑上限。\n数值格式：decimals、natural_decimals、min_integer_digits、unit、unit_enabled、minimum、maximum，只用于输入框(0x5025)。\n按钮动作：action_variable 改已有动作的目标，action_mode 改 5 种模式之一；按钮没有动作时无法新增。\n字段用错类型返回 UNSUPPORTED_CONTROL。写操作，整页重建，约 10-15 秒。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "目标窗口名"},
                "index": {"type": "integer", "description": "控件序号，从 0 开始"},
                "caption": {"type": "string", "description": "新的显示文字，可选"},
                "x": {"type": "integer", "description": "新的左上角 X，可选"},
                "y": {"type": "integer", "description": "新的左上角 Y，可选"},
                "width": {"type": "integer", "description": "新的框宽，可选"},
                "height": {"type": "integer", "description": "新的框高，可选"},
                "font_size": {"type": "integer", "description": "字号，可选"},
                "font_face": {"type": "string", "description": "字体名，可选"},
                "font_weight": {"type": "integer", "description": "字重，可选"},
                "fill": {"type": "string", "description": "填充色或 \"none\"，可选"},
                "text_color": {"type": "string", "description": "文字颜色，可选"},
                "border_color": {"type": "string", "description": "边框颜色，可选"},
                "border_width": {"type": "integer", "description": "边框线宽，可选"},
                "text_off": {"type": "string",
                             "description": "开关/指示灯的「关」状态文字，可选"},
                "text_on": {"type": "string",
                            "description": "开关/指示灯的「开」状态文字，可选"},
                "text_font": {"type": "string",
                              "description": "开关/指示灯文字的字体名，可选"},
                "action_variable": {"type": "string",
                                    "description": "改按钮动作的目标变量（按钮必须已有动作），可选"},
                "action_mode": {"type": "string",
                                "enum": ["置1", "置0", "取反", "按1松0", "按0松1"],
                                "description": "按钮动作的 5 种模式之一，可选"},
                "variable": {"type": "string",
                             "description": (
                            "要绑定的设备通道名，例如 设备0_读写DWUB1000。"
                            "必须是 mcgs_get_device 列出的通道，否则报 UNKNOWN_VARIABLE。"
                        ),},
                "max_variable": {"type": "string",
                                 "description": (
                            "数值输入的上限变量（第二个变量槽），例如 "
                            "取 mcgs_get_device 返回的 channel 值。"
                            "只有数值输入有这个槽；其他类型会报 UNSUPPORTED_CONTROL。"
                        ),},
                "decimals": {"type": "integer", "minimum": 0, "maximum": 8,
                             "description": "输入框固定小数位数（会自动关掉自然小数），可选"},
                "natural_decimals": {"type": "boolean",
                                     "description": "输入框是否用自然小数，可选"},
                "min_integer_digits": {"type": "integer", "minimum": 0, "maximum": 8,
                                       "description": "输入框最小整数位数，可选"},
                "unit": {"type": "string", "description": "输入框单位文字，可选"},
                "unit_enabled": {"type": "boolean",
                                 "description": "输入框是否显示单位，可选"},
                "minimum": {"type": "number", "description": "输入框最小值，可选"},
                "maximum": {"type": "number", "description": "输入框最大值，可选"},
                "project": {
                    "type": "string",
                    "description": (
                        "要操作的工程。必填。可以是完整路径、工程名，"
                        "或能唯一匹配的片段。"
                    ),
                },
            },
            "required": ["window", "index", "project"],
            "additionalProperties": False,
        },
    },
    "mcgs_controls_update": {
        "fn": _guarded(labels_mod.update_many),
        "description": (
            '一次修改多个控件，只重建一次页面。\n必填：window、updates、project。updates 是数组，每项必填 index，其余字段与 mcgs_control_update 相同。\n整页重建要过一遍剪贴板，控件多或窗口大时逐个改会非常慢，批量是常规用法。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string",
                           "description": "窗口名。必填。"},
                "updates": {
                    "type": "array",
                    "description": "要修改的控件列表，每项至少含 index。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer",
                                      "description": "控件序号，见 mcgs_get_window 返回的 index"},
                            "caption": {"type": "string"},
                            "x": {"type": "integer"}, "y": {"type": "integer"},
                            "width": {"type": "integer"}, "height": {"type": "integer"},
                            "font_size": {"type": "integer"},
                            "font_face": {"type": "string"},
                            "font_weight": {"type": "integer"},
                            "fill": {"type": "string"},
                            "text_color": {"type": "string"},
                            "border_color": {"type": "string"},
                            "border_width": {"type": "integer"},
                            "text_off": {"type": "string"},
                            "text_on": {"type": "string"},
                            "text_font": {"type": "string"},
                            "action_variable": {"type": "string"},
                            "action_mode": {"type": "string",
                                            "enum": ["置1", "置0", "取反", "按1松0", "按0松1"]},
                            "variable": {"type": "string",
                                         "description": "要绑定的设备通道名，见 mcgs_get_device"},
                            "max_variable": {"type": "string",
                                             "description": "数值输入的上限变量（第二个变量槽）"},
                            "decimals": {"type": "integer", "minimum": 0, "maximum": 8},
                            "natural_decimals": {"type": "boolean"},
                            "min_integer_digits": {"type": "integer", "minimum": 0, "maximum": 8},
                            "unit": {"type": "string"},
                            "unit_enabled": {"type": "boolean"},
                            "minimum": {"type": "number"},
                            "maximum": {"type": "number"},
                        },
                        "required": ["index"],
                        "additionalProperties": False,
                    },
                },
                "project": {
                    "type": "string",
                    "description": (
                        "要操作的工程。必填。可以是完整路径、工程名，"
                        "或能唯一匹配的片段。"
                    ),
                },
            },
            "required": ["window", "updates", "project"],
            "additionalProperties": False,
        },
    },
    "mcgs_control_delete": {
        "fn": _guarded(labels_mod.delete_control),
        "description": (
            '删除窗口里的一个控件并保存。任意类型。\n必填：window、index、project。\n删除后其余控件的 index 会前移。写操作，整页重建，约 10-15 秒。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "目标窗口名"},
                "index": {"type": "integer", "description": "要删除的控件序号，从 0 开始"},
                "project": {
                    "type": "string",
                    "description": (
                        "要操作的工程。必填。可以是完整路径、工程名，"
                        "或能唯一匹配的片段。"
                    ),
                },
            },
            "required": ["window", "index", "project"],
            "additionalProperties": False,
        },
    },
    "mcgs_control_add": {
        "fn": _guarded(labels_mod.add_control),
        "description": (
            '在窗口里添加一个非文字控件并保存。\n必填：window、kind、x、y、width、height、project。\nkind：button（标准按钮）/ bit_switch（开关）/ lamp（指示灯）/ numeric_input（输入框）。\n可选：name、fill、border_color、border_width、text_off、text_on、text_font。\n这几种控件的外观由模板内嵌图片绘制，颜色参数会写进去但界面不显示（返回值 warnings 会提示）。新建控件默认未绑变量，需要再用 mcgs_control_update 绑定。写操作，整页重建，约 10-15 秒。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "description": "目标窗口名"},
                "kind": {"type": "string",
                         "enum": ["button", "bit_switch", "lamp", "numeric_input"],
                         "description": "控件类型"},
                "x": {"type": "integer", "description": "左上角 X 坐标"},
                "y": {"type": "integer", "description": "左上角 Y 坐标"},
                "width": {"type": "integer", "description": "框宽度"},
                "height": {"type": "integer", "description": "框高度"},
                "name": {"type": "string", "description": "控件名，可选"},
                "fill": {"type": "string", "description": "填充色或 \"none\"，可选"},
                "border_color": {"type": "string", "description": "边框颜色，可选"},
                "border_width": {"type": "integer", "description": "边框线宽，可选"},
                "text_off": {"type": "string",
                             "description": "开关/指示灯的「关」状态文字，可选"},
                "text_on": {"type": "string",
                            "description": "开关/指示灯的「开」状态文字，可选"},
                "text_font": {"type": "string",
                              "description": "开关/指示灯文字的字体名，可选"},
                "project": {
                    "type": "string",
                    "description": (
                        "要操作的工程。必填。可以是完整路径、工程名，"
                        "或能唯一匹配的片段。"
                    ),
                },
            },
            "required": ["window", "kind", "x", "y", "width", "height", "project"],
            "additionalProperties": False,
        },
    },
}

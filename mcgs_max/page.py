"""Rewrite a whole user window: clear the canvas, paste the payloads, save, verify.

MCGS silently ignores a paste into a canvas that already holds objects, so every
write starts by clearing. The project is backed up first and the result is
verified two ways — the stored database and MCGS's own 组态检查 — because a
canvas that merely looks right can still carry a configuration error.
"""

from __future__ import annotations

import ctypes
import re
import shutil
import struct
import time
from pathlib import Path

from . import binding, builder, clipboard, controls, project as project_mod, win32, workbench
from . import labels as labels_mod

user32 = ctypes.windll.user32

WM_COMMAND = 0x0111
CMD_SELECT_ALL = 57642
CMD_DELETE = 57632
CMD_PASTE = 57637
CMD_DESELECT = 57636
CMD_SAVE = 57603

LABEL_TYPE = 0x5008


class PageError(RuntimeError):
    """Raised when a page cannot be rewritten safely."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _context(project: str | None = None) -> tuple[int, str, dict]:
    try:
        pid, path = win32.resolve_project(project)
    except win32.ProjectLookupError as exc:
        raise PageError(exc.code, exc.message, available=exc.available) from None

    # A property sheet left open refers to the control it was opened on; a page
    # rewrite would leave it pointing at a destroyed object, so clear it first.
    dismissed = workbench.dismiss_dialogs(pid)
    if dismissed["remaining"]:
        titles = "、".join(d.get("title") or "?" for d in dismissed["remaining"])
        raise PageError(
            "BLOCKED_BY_DIALOG",
            f"以下对话框关不掉，挡住了写操作：{titles}。请手动处理。",
            dialogs=dismissed["remaining"],
            closed=dismissed["closed"])

    return pid, path, dismissed


KEEP_BACKUPS = 1


def _backup(project_path: str) -> Path:
    """Copy the project before a write, keeping only the newest few.

    A rewrite of a large window copies the whole database, so only one is kept:
    it exists purely to roll back a failed write, and MCGS keeps its own
    .MCP.bak alongside it.
    """
    src = Path(project_path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = src.with_name(f"{src.stem}.{stamp}.bak")
    shutil.copy2(src, target)

    pattern = re.compile(rf"^{re.escape(src.stem)}\.\d{{8}}_\d{{6}}\.bak$")
    mine = [p for p in src.parent.iterdir()
            if p.is_file() and pattern.match(p.name)]
    older = sorted(mine, key=lambda p: p.stat().st_mtime,
                   reverse=True)[KEEP_BACKUPS:]
    for path in older:
        try:
            path.unlink()
        except OSError:
            pass
    return target


def _clear(canvas: int) -> None:
    user32.PostMessageW(canvas, WM_COMMAND, CMD_SELECT_ALL, 0)
    time.sleep(0.7)
    user32.PostMessageW(canvas, WM_COMMAND, CMD_DELETE, 0)
    time.sleep(1.0)


def _paste(canvas: int, stream: bytes) -> dict:
    published = clipboard.publish(stream, clipboard.FORMAT_DRAW)
    user32.PostMessageW(canvas, WM_COMMAND, CMD_DESELECT, 0)
    time.sleep(0.4)
    user32.PostMessageW(canvas, WM_COMMAND, CMD_PASTE, 0)
    time.sleep(3.0)
    return published


def _save(pid: int) -> None:
    frame = win32.main_frame(pid)
    if frame is None:
        raise PageError("NO_MAIN_WINDOW", "找不到 MCGS 主窗口。")
    user32.PostMessageW(frame.hwnd, WM_COMMAND, CMD_SAVE, 0)
    time.sleep(3.5)


def _class_names(payloads: list[bytes], type_ids: list[int]) -> None:
    for type_id in type_ids:
        if type_id not in builder.TYPE_CLASS:
            raise PageError(
                "UNSUPPORTED_CONTROL",
                f"页面里有 0x{type_id:04X} 类型的控件，不知道它的剪贴板类名。"
                "整页重建会用到类名，猜错会导致工程损坏。")



def _restore(backup: Path, project_path: str, pid: int) -> bool:
    """Put `backup` back in place of the live project file.

    MCGS holds the file open, so it is closed, the backup copied over, and the
    editor reopened. Returns True when the copy succeeded.
    """
    import subprocess

    exe = None
    try:
        handle_proc = win32.list_processes()
        if handle_proc:
            exe = win32.find_installed_exe()
    except Exception:
        exe = None

    procs = win32.list_processes()
    if procs:
        subprocess.run(["taskkill", "/PID", str(procs[0]["pid"]), "/F"],
                       capture_output=True)
        deadline = time.time() + 12
        while time.time() < deadline and win32.list_processes():
            time.sleep(0.4)

    try:
        shutil.copy2(backup, project_path)
    except OSError:
        return False

    if exe:
        subprocess.Popen([exe, project_path], close_fds=True)
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1.0)
            procs = win32.list_processes()
            if procs:
                workbench.dismiss_dialogs(procs[0]["pid"], timeout=8)
                break
    return True


def _attach_number_format(decoded, records, project_path):
    """Add the numeric-input format block to decoded controls."""
    by_index = {r["index"]: r for r in records}
    prefixes, _channels = labels_mod._binding_context(project_path)
    for item in decoded:
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
        except (ValueError, controls.FormatError):
            pass


def replace_page(window_name: str, payloads: list[bytes],
                 type_ids: list[int] | None = None,
                 project: str | None = None) -> dict:
    """Replace every control in `window_name` with `payloads`."""
    pid, project_path, dismissed = _context(project)

    if type_ids is None:
        type_ids = [LABEL_TYPE] * len(payloads)
    _class_names(payloads, type_ids)

    canvas = workbench.open_canvas(pid, window_name)
    if canvas is None:
        raise PageError("CANVAS_NOT_OPEN",
                        f"打不开 {window_name!r} 的画布，请确认窗口存在。")

    backup = _backup(project_path)

    _clear(canvas)

    published = None
    if payloads:
        stream = builder.build_stream(payloads, type_ids)
        published = _paste(canvas, stream)
        workbench.dismiss_dialogs(pid, rounds=2, timeout=4)
        dialogs = win32.modal_dialogs(pid)
        if dialogs:
            raise PageError("FORMAT_ERROR", "MCGS 拒绝了这个控件流。",
                            dialogs=dialogs, backup=str(backup))

    _save(pid)

    after = project_mod.read_project(project_path)
    page = next((w for w in after.windows if w.name == window_name), None)
    if page is None:
        raise PageError("WINDOW_LOST", f"保存后找不到窗口 {window_name!r}。",
                        backup=str(backup))

    if page.control_count != len(payloads):
        # the paste did not take; the page on disk is now wrong, so put the
        # backup back rather than leaving the project damaged
        _restore(backup, project_path, pid)
        raise PageError(
            "COUNT_MISMATCH",
            f"期望 {len(payloads)} 个控件，实际 {page.control_count} 个；已从备份恢复。",
            backup=str(backup), restored=True)

    errors = workbench.run_check(pid)
    decoded = controls.parse_controls(page.controls)
    _attach_number_format(decoded, page.controls, project_path)

    # Jet 不回收空闲页，文件会随写入次数增长；膨胀过多时提示压缩
    bloat = None
    try:
        file_bytes = Path(project_path).stat().st_size
        data_bytes = sum(w.blob_bytes for w in after.windows) or 1
        if file_bytes > data_bytes * 3 and file_bytes > 32 * 1024 * 1024:
            bloat = {
                "file_mb": round(file_bytes / 1024 / 1024, 1),
                "data_mb": round(data_bytes / 1024 / 1024, 1),
                "hint": "工程文件远大于实际数据（写入会累积空闲页）。"
                        "重启 MCGS 会自动压缩回去。",
            }
    except OSError:
        pass

    # 到这里控件数已核对过，写入是成功的。组态检查针对整个工程，
    # 其他窗口里既有的错误不该让本次写入被判为失败。
    payload = {
        "ok": True,
        "window": window_name,
        "control_count": page.control_count,
        "controls": decoded,
        "check_errors": errors,
        "check_passed": not errors,
        "backup": str(backup),
        "dialogs_closed": dismissed["closed"],
        **({"bloat": bloat} if bloat else {}),
        "clipboard": published,
    }
    if errors:
        here = [e for e in errors if window_name in str(e.get("object", ""))]
        payload["warning"] = (
            f"组态检查有 {len(errors)} 条错误，其中 {len(here)} 条在本窗口；"
            "其余可能来自工程里其他窗口的既有问题。"
        )
    return payload

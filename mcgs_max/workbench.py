"""Drive the MCGS workbench: window list, opening a canvas, configuration check.

Almost everything here works through window handles and standard control
messages — no cursor movement, no screen coordinates. The one exception is
opening a canvas in a window that is not active: posted click messages are
ignored there, so a real double-click is used as a fallback and the cursor is
restored immediately afterwards.

MCGS is a 32-bit process, so LVITEM is laid out with 32-bit pointers even when
this runs under 64-bit Python.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct
import time

from . import win32


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

LVM_GETITEMCOUNT = 0x1004
LVM_SETITEMSTATE = 0x1017 + 26          # 0x102B
LVM_GETITEMTEXTW = 0x1073
LVM_GETHEADER = 0x101F
HDM_GETITEMCOUNT = 0x1200

LVIF_TEXT = 0x0001
LVIF_STATE = 0x0008
LVIS_SELECTED = 0x0002
LVIS_FOCUSED = 0x0001

WM_COMMAND = 0x0111
CMD_CHECK = 32786                        # 组态检查 (F4)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

CHECKLIST_TITLE = "组态检查"


class LVITEM32(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("mask", ctypes.c_uint32), ("iItem", ctypes.c_int32),
        ("iSubItem", ctypes.c_int32), ("state", ctypes.c_uint32),
        ("stateMask", ctypes.c_uint32), ("pszText", ctypes.c_uint32),
        ("cchTextMax", ctypes.c_int32), ("iImage", ctypes.c_int32),
        ("lParam", ctypes.c_uint32), ("iIndent", ctypes.c_int32),
        ("iGroupId", ctypes.c_int32), ("cColumns", ctypes.c_uint32),
        ("puColumns", ctypes.c_uint32), ("piColFmt", ctypes.c_uint32),
        ("iGroup", ctypes.c_int32),
    ]


def _pid_of(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _open_process(pid: int):
    handle = kernel32.OpenProcess(
        PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError(f"OpenProcess failed for pid {pid}")
    return handle


def _item_count(list_hwnd: int) -> int:
    user32.SendMessageW.restype = ctypes.c_ssize_t
    return int(user32.SendMessageW(list_hwnd, LVM_GETITEMCOUNT, 0, 0))


def read_list(list_hwnd: int) -> list[list[str]]:
    """Every row and column of a ListView."""
    count = _item_count(list_hwnd)
    if count <= 0:
        return []

    header = int(user32.SendMessageW(list_hwnd, LVM_GETHEADER, 0, 0))
    columns = int(user32.SendMessageW(header, HDM_GETITEMCOUNT, 0, 0)) if header else 1
    columns = max(columns, 1)

    proc = _open_process(_pid_of(list_hwnd))
    remote = None
    try:
        item_size = ctypes.sizeof(LVITEM32)
        chars = 512
        buf_size = chars * 2
        remote = kernel32.VirtualAllocEx(proc, None, item_size + buf_size,
                                         MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not remote:
            raise OSError("VirtualAllocEx failed")
        text_addr = remote + item_size

        rows = []
        for row_index in range(count):
            row = []
            for sub in range(columns):
                item = LVITEM32()
                item.mask = LVIF_TEXT
                item.iItem = row_index
                item.iSubItem = sub
                item.pszText = text_addr
                item.cchTextMax = chars

                written = ctypes.c_size_t(0)
                kernel32.WriteProcessMemory(proc, ctypes.c_void_p(remote),
                                            ctypes.byref(item), item_size,
                                            ctypes.byref(written))
                blank = b"\x00" * buf_size
                kernel32.WriteProcessMemory(proc, ctypes.c_void_p(text_addr),
                                            blank, buf_size, ctypes.byref(written))

                user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
                result = ctypes.c_ssize_t(0)
                user32.SendMessageTimeoutW(list_hwnd, LVM_GETITEMTEXTW, row_index,
                                           ctypes.c_void_p(remote), 0x0002, 2000,
                                           ctypes.byref(result))

                buf = ctypes.create_string_buffer(buf_size)
                read = ctypes.c_size_t(0)
                kernel32.ReadProcessMemory(proc, ctypes.c_void_p(text_addr), buf,
                                           buf_size, ctypes.byref(read))
                raw = buf.raw.split(b"\x00\x00")[0]
                if len(raw) % 2:
                    raw += b"\x00"
                row.append(raw.decode("utf-16le", errors="replace"))
            rows.append(row)
        return rows
    finally:
        if remote:
            kernel32.VirtualFreeEx(proc, ctypes.c_void_p(remote), 0, MEM_RELEASE)
        kernel32.CloseHandle(proc)


def select_item(list_hwnd: int, index: int) -> None:
    """Select and focus one row of a foreign ListView."""
    proc = _open_process(_pid_of(list_hwnd))
    remote = None
    try:
        size = ctypes.sizeof(LVITEM32)
        remote = kernel32.VirtualAllocEx(proc, None, size, MEM_COMMIT | MEM_RESERVE,
                                         PAGE_READWRITE)
        if not remote:
            raise OSError("VirtualAllocEx failed")
        item = LVITEM32()
        item.mask = LVIF_STATE
        item.iItem = index
        item.state = LVIS_SELECTED | LVIS_FOCUSED
        item.stateMask = LVIS_SELECTED | LVIS_FOCUSED
        written = ctypes.c_size_t(0)
        kernel32.WriteProcessMemory(proc, ctypes.c_void_p(remote),
                                    ctypes.byref(item), size, ctypes.byref(written))
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW(list_hwnd, LVM_SETITEMSTATE, index,
                            ctypes.c_void_p(remote))
    finally:
        if remote:
            kernel32.VirtualFreeEx(proc, ctypes.c_void_p(remote), 0, MEM_RELEASE)
        kernel32.CloseHandle(proc)


def window_list(pid: int) -> tuple[int | None, list[str]]:
    """The workbench's window list and its captions."""
    workbench = win32.workbench_window(pid)
    if workbench is None:
        return None, []
    for child in win32.child_windows(workbench.hwnd):
        if child.cls == "SysListView32" and child.visible:
            rows = read_list(child.hwnd)
            return child.hwnd, [r[0] if r else "" for r in rows]
    return None, []


def item_position(list_hwnd: int, index: int) -> tuple[int, int]:
    """Top-left of an item in the ListView's client coordinates."""
    proc = _open_process(_pid_of(list_hwnd))
    remote = None
    try:
        remote = kernel32.VirtualAllocEx(proc, None, 8, MEM_COMMIT | MEM_RESERVE,
                                         PAGE_READWRITE)
        if not remote:
            raise OSError("VirtualAllocEx failed")
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW(list_hwnd, 0x1010, index, ctypes.c_void_p(remote))
        buf = ctypes.create_string_buffer(8)
        read = ctypes.c_size_t(0)
        kernel32.ReadProcessMemory(proc, ctypes.c_void_p(remote), buf, 8,
                                   ctypes.byref(read))
        return struct.unpack("<2i", buf.raw)
    finally:
        if remote:
            kernel32.VirtualFreeEx(proc, ctypes.c_void_p(remote), 0, MEM_RELEASE)
        kernel32.CloseHandle(proc)


CANVAS_BUTTON = "\u52a8\u753b\u7ec4\u6001"      # 动画组态


TCM_GETITEMCOUNT = 0x1304
TCM_GETCURSEL = 0x130B
USER_TAB_HINTS = ("设备窗口", "主控窗口", "实时数据库", "运行策略")


def _tab_control(pid: int):
    wb = win32.workbench_window(pid)
    if wb is None:
        return None
    return next((c for c in win32.child_windows(wb.hwnd)
                 if c.cls == "SysTabControl32"), None)


def _click_tab(tab_hwnd: int, index: int, count: int, width: int) -> None:
    """Post a click at the centre of tab `index`."""
    x = int(width * (index + 0.5) / max(count, 1))
    spot = (14 << 16) | x
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW(tab_hwnd, 0x0201, 1, spot)      # WM_LBUTTONDOWN
    time.sleep(0.05)
    user32.SendMessageW(tab_hwnd, 0x0202, 0, spot)      # WM_LBUTTONUP
    time.sleep(0.9)


def switch_to_user_tab(pid: int, wanted: str) -> bool:
    """Switch the workbench to the tab whose list contains `wanted`."""
    tab = _tab_control(pid)
    if tab is None:
        return False
    user32.SendMessageW.restype = ctypes.c_ssize_t
    count = int(user32.SendMessageW(tab.hwnd, TCM_GETITEMCOUNT, 0, 0))
    if count <= 0:
        return False
    rect = wt.RECT()
    if not user32.GetClientRect(tab.hwnd, ctypes.byref(rect)):
        return False
    width = rect.right - rect.left
    current = int(user32.SendMessageW(tab.hwnd, TCM_GETCURSEL, 0, 0))

    # 用户窗口 通常是第 3 个，先试它，再逐个试其余
    order = [2] + [i for i in range(count) if i != 2]
    for index in order:
        if index == current:
            continue
        _click_tab(tab.hwnd, index, count, width)
        _lst, names = window_list(pid)
        if wanted in names:
            return True
    return False


def open_canvas(pid: int, window_name: str, timeout: float = 10.0) -> int | None:
    """Open a user window's animation canvas and return its handle.

    Neither Enter on the list nor the 动画组态 button opens it in this build; a
    double-click on the item does. Posting the click messages is enough when
    MCGS already has focus, but a window that is not active ignores them, so a
    real double-click is used as a fallback — the cursor is put back afterwards.
    """
    frame = win32.main_frame(pid)
    if frame is None:
        return None

    def find() -> int | None:
        for child in win32.child_windows(frame.hwnd):
            if (child.cls == "AfxFrameOrView110u" and child.visible
                    and window_name in child.title):
                return child.hwnd
        return None

    existing = find()
    if existing:
        return existing

    lst, names = window_list(pid)
    if window_name not in names:
        # 工作台可能停在别的页签（设备窗口等），切回用户窗口再试
        if switch_to_user_tab(pid, window_name):
            lst, names = window_list(pid)
    if lst is None or window_name not in names:
        return None

    index = names.index(window_name)
    select_item(lst, index)
    time.sleep(0.4)

    x, y = item_position(lst, index)
    spot = ((y + 8) << 16) | (x + 20)
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW(lst, 0x0201, 1, spot)      # WM_LBUTTONDOWN
    user32.SendMessageW(lst, 0x0202, 0, spot)      # WM_LBUTTONUP
    time.sleep(0.05)
    user32.SendMessageW(lst, 0x0203, 1, spot)      # WM_LBUTTONDBLCLK

    deadline = time.time() + timeout / 2
    while time.time() < deadline:
        time.sleep(0.4)
        found = find()
        if found:
            return found

    # The posted messages were ignored; drive a real double-click instead.
    if _double_click_item(frame.hwnd, lst, x, y):
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            found = find()
            if found:
                return found
    return None


def _double_click_item(frame_hwnd: int, list_hwnd: int, x: int, y: int) -> bool:
    """Real double-click on a list item, restoring the cursor afterwards."""
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return False
    origin = POINT(0, 0)
    if not user32.ClientToScreen(list_hwnd, ctypes.byref(origin)):
        return False
    target_x = origin.x + x + 20
    target_y = origin.y + y + 8
    if target_x < -10000 or target_y < -10000:
        return False                       # window is minimised, nothing to click

    try:
        user32.SetForegroundWindow(frame_hwnd)
        time.sleep(0.5)
        user32.SetCursorPos(target_x, target_y)
        time.sleep(0.25)
        for _ in range(2):
            user32.mouse_event(0x0002, 0, 0, 0, 0)      # LEFTDOWN
            time.sleep(0.05)
            user32.mouse_event(0x0004, 0, 0, 0, 0)      # LEFTUP
            time.sleep(0.10)
    finally:
        user32.SetCursorPos(point.x, point.y)
    return True


# Buttons that dismiss a dialog. Cancelling is preferred because it applies
# nothing; accepting a property sheet would push whatever is in it onto the
# control, which is not what the caller asked for.
DISMISS_PREFERENCE = ("取消", "否", "关闭", "确定")


def _caption(text: str) -> str:
    """Button caption with the accelerator and all padding spaces removed.

    MCGS centres captions by inserting spaces — "确       认", "取       消" —
    so a plain startswith on the visible text never matches.
    """
    return "".join(text.replace("&", "").split())


def dismiss_dialogs(pid: int, rounds: int = 6, timeout: float = 12.0) -> dict:
    """Close dialogs that would block a write.

    Returns which ones were closed and which refused to go. Non-destructive
    buttons are tried first, so a property sheet is cancelled rather than
    applied.
    """
    closed: list[str] = []
    deadline = time.time() + timeout

    for _ in range(rounds):
        dialogs = win32.modal_dialogs(pid)
        if not dialogs:
            return {"closed": closed, "remaining": []}

        progressed = False
        for dialog in dialogs:
            buttons = [c for c in win32.child_windows(dialog["hwnd"])
                       if c.cls == "Button" and c.title.strip()]
            chosen = None
            for want in DISMISS_PREFERENCE:
                chosen = next((b for b in buttons
                               if _caption(b.title).startswith(want)), None)
                if chosen:
                    break
            if chosen is None:
                continue
            user32.SendMessageW.restype = ctypes.c_ssize_t
            user32.SendMessageW(chosen.hwnd, 0x00F5, 0, 0)     # BM_CLICK
            closed.append(f"{dialog.get('title') or '?'} <- {chosen.title}")
            progressed = True
            time.sleep(0.8)

        if not progressed or time.time() > deadline:
            break

    remaining = win32.modal_dialogs(pid)
    return {"closed": closed, "remaining": remaining}


def _dismiss_result(pid: int, timeout: float = 4.0) -> bool:
    """Close the '组态检查通过' confirmation MCGS shows after a check."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        target = None
        for dialog in win32.modal_dialogs(pid):
            text = dialog.get("text", "")
            if "组态检查" in text or "发现错误" in text:
                target = dialog
                break
        if target is None:
            return True
        button = next((c for c in win32.child_windows(target["hwnd"])
                       if c.cls == "Button"
                       and c.title.replace("&", "").startswith("确定")), None)
        if button is None:
            return False
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW(button.hwnd, 0x00F5, 0, 0)      # BM_CLICK
        time.sleep(0.8)
    return False


def run_check(pid: int, timeout: float = 8.0) -> list[dict]:
    """Run 组态检查, collect its rows, and close the confirmation it pops up.

    Each row is [object path, level, ?, message]. An empty list means MCGS found
    nothing to report.
    """
    frame = win32.main_frame(pid)
    if frame is None:
        return []
    user32.PostMessageW(frame.hwnd, WM_COMMAND, CMD_CHECK, 0)

    deadline = time.time() + timeout
    target = None
    while time.time() < deadline:
        time.sleep(0.4)
        target = next((w for w in win32.top_level_windows(pid)
                       if CHECKLIST_TITLE in w.title), None)
        if target:
            break

    errors: list[dict] = []
    if target:
        lst = next((c.hwnd for c in win32.child_windows(target.hwnd)
                    if c.cls == "SysListView32"), None)
        if lst is not None:
            for row in read_list(lst):
                if not any(cell.strip() for cell in row):
                    continue
                errors.append({
                    "object": row[0] if len(row) > 0 else "",
                    "level": row[1] if len(row) > 1 else "",
                    "message": row[3] if len(row) > 3 else " | ".join(row[1:]),
                })

    _dismiss_result(pid)
    return errors

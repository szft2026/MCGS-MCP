"""Publish a byte stream to one of MCGS's named clipboard formats.

MCGS does not expose an API, so the only way to hand it a batch of controls is
its own clipboard format. Writing that format is straightforward; the part that
matters is verifying every byte landed, because a short or altered payload shows
up in MCGS as 格式错误 with no further detail.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

FORMAT_DRAW = "MCGS_DRAW_OBJ_PRO1"
FORMAT_WINDOW = "MCGS_WND_USER_PRO1"
FORMATS = (FORMAT_DRAW, FORMAT_WINDOW)

GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
MAX_PAYLOAD = 16 * 1024 * 1024


def _bind(lib, name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_Open = _bind(user32, "OpenClipboard", wt.BOOL, wt.HWND)
_Close = _bind(user32, "CloseClipboard", wt.BOOL)
_Empty = _bind(user32, "EmptyClipboard", wt.BOOL)
_Register = _bind(user32, "RegisterClipboardFormatW", wt.UINT, wt.LPCWSTR)
_Set = _bind(user32, "SetClipboardData", wt.HANDLE, wt.UINT, wt.HANDLE)
_Get = _bind(user32, "GetClipboardData", wt.HANDLE, wt.UINT)
_Sequence = _bind(user32, "GetClipboardSequenceNumber", wt.DWORD)
_CreateWindow = _bind(
    user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR,
    wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p)
_DestroyWindow = _bind(user32, "DestroyWindow", wt.BOOL, wt.HWND)
_Alloc = _bind(kernel32, "GlobalAlloc", wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
_Free = _bind(kernel32, "GlobalFree", wt.HGLOBAL, wt.HGLOBAL)
_Lock = _bind(kernel32, "GlobalLock", ctypes.c_void_p, wt.HGLOBAL)
_Unlock = _bind(kernel32, "GlobalUnlock", wt.BOOL, wt.HGLOBAL)
_Size = _bind(kernel32, "GlobalSize", ctypes.c_size_t, wt.HGLOBAL)


def _required(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def sequence() -> int:
    """Current clipboard sequence number, for detecting concurrent changes."""
    return int(_Sequence())


def publish(data: bytes, format_name: str = FORMAT_DRAW) -> dict:
    """Put `data` on the clipboard under MCGS's format and read it back."""
    if format_name not in FORMATS:
        raise ValueError(f"unsupported clipboard format: {format_name}")
    if not 0 < len(data) <= MAX_PAYLOAD:
        raise ValueError(f"invalid payload size: {len(data)}")

    expected_sequence = sequence()
    window = _required(_CreateWindow(
        0, "STATIC", "mcgs-max clipboard", 0, 0, 0, 0, 0,
        wt.HWND(-3), None, None, None))          # HWND_MESSAGE
    memory = None
    opened = False
    transferred = False
    try:
        fmt = _required(_Register(format_name))
        memory = _required(_Alloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data)))
        pointer = _required(_Lock(memory))
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            _Unlock(memory)

        for _ in range(10):
            if _Open(window):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            raise RuntimeError("剪贴板被其它程序占用，请重试")

        if sequence() != expected_sequence:
            raise RuntimeError("准备期间剪贴板内容已变化，请重试")

        _required(_Empty())
        _required(_Set(fmt, memory))
        transferred = True

        handle = _required(_Get(fmt))
        size = _Size(handle)
        if not len(data) <= size <= MAX_PAYLOAD:
            raise RuntimeError(f"剪贴板分配大小异常: {size}")

        pointer = _required(_Lock(handle))
        try:
            readback = ctypes.string_at(pointer, size)
        finally:
            _Unlock(handle)

        if readback[:len(data)] != data or any(readback[len(data):]):
            raise RuntimeError("剪贴板回读校验失败")

        return {
            "format": format_name,
            "bytes": len(data),
            "allocated_bytes": size,
            "sha256": hashlib.sha256(data).hexdigest(),
            "exact_readback": True,
        }
    finally:
        if opened:
            _Close()
        if memory and not transferred:
            _Free(memory)
        _DestroyWindow(window)

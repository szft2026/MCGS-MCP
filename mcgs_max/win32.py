"""Thin Win32 helpers for talking to a running McgsPro installation.

Everything here is discovered at runtime: process id, window handles, project
path. Nothing is hard-coded to a particular machine or install location.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.windll.user32

PROCESS_IMAGE = "McgsSetPro.exe"
CANVAS_CLASS = "AfxFrameOrView110u"

# 工作台 : <path>   with an optional trailing * meaning "unsaved changes"
WORKBENCH_RE = re.compile(r"^\s*\u5de5\u4f5c\u53f0\s*[:\uff1a]\s*(.+?)(\*?)\s*$")
_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def looks_like_path(value: str) -> bool:
    """A drive letter path, a UNC path, or a name ending in .MCP."""
    if not value:
        return False
    if _PATH_RE.match(value):
        return True
    return value.lower().endswith(".mcp")


WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


@dataclass(frozen=True)
class WinInfo:
    hwnd: int
    title: str
    cls: str
    visible: bool
    pid: int


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


# ---------------------------------------------------------------- processes
def list_processes() -> list[dict]:
    """All running MCGS editor processes."""
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {PROCESS_IMAGE}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    found = []
    for line in out.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("mcgs"):
            try:
                found.append({"pid": int(parts[1]), "image": parts[0]})
            except ValueError:
                continue
    return found


# ---------------------------------------------------------------- windows
def top_level_windows(pid: int | None = None) -> list[WinInfo]:
    """Every top-level window, optionally filtered to one process."""
    result: list[WinInfo] = []

    def _cb(hwnd, _lp):
        if pid is not None and _window_pid(hwnd) != pid:
            return True
        result.append(WinInfo(hwnd, _window_text(hwnd), _window_class(hwnd),
                              bool(user32.IsWindowVisible(hwnd)), _window_pid(hwnd)))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return result


def child_windows(hwnd: int) -> list[WinInfo]:
    """Every descendant window of `hwnd`."""
    result: list[WinInfo] = []

    def _cb(child, _lp):
        result.append(WinInfo(child, _window_text(child), _window_class(child),
                              bool(user32.IsWindowVisible(child)), _window_pid(child)))
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(_cb), 0)
    return result


def main_frame(pid: int) -> WinInfo | None:
    """The real editor frame.

    Several windows can carry the product name (message-only frames, dialogs),
    so the one with the most descendants wins.
    """
    candidates = [w for w in top_level_windows(pid) if w.visible and "McgsPro" in w.title]
    if not candidates:
        return None
    best, best_count = None, -1
    for w in candidates:
        count = len(child_windows(w.hwnd))
        if count > best_count:
            best, best_count = w, count
    return best


def find_button(parent: int, caption: str) -> int | None:
    """Locate a child button by caption, ignoring accelerator markers."""
    wanted = caption.replace("&", "")
    for child in child_windows(parent):
        if child.cls == "Button" and child.title.replace("&", "").strip() == wanted:
            return child.hwnd
    return None


def click_button(hwnd: int) -> bool:
    """Send BM_CLICK.

    This reaches the control regardless of z-order, which matters because the
    animation canvas can cover the whole frame.
    """
    BM_CLICK = 0x00F5
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW(hwnd, BM_CLICK, 0, 0)
    return True



def find_installed_exe() -> str | None:
    """Locate McgsSetPro.exe without a running process.

    Checks the App Paths registry entry first (written by the installer), then
    the usual install roots, so nothing is hard-coded to one machine.
    """
    import string
    import winreg
    from pathlib import Path

    for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        for root in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"):
            try:
                key = winreg.OpenKey(hive, rf"{root}\{PROCESS_IMAGE}", 0,
                                     winreg.KEY_READ | view)
            except OSError:
                continue
            try:
                value, _ = winreg.QueryValueEx(key, "")
                if value and Path(value).exists():
                    return value
            except OSError:
                pass
            finally:
                key.Close()

    for letter in string.ascii_uppercase:
        base = Path(f"{letter}:\\")
        if not base.exists():
            continue
        for name in ("McgsPro", "MCGS"):
            for candidate in (base / name / "Program" / PROCESS_IMAGE,
                              base / name / PROCESS_IMAGE,
                              base / "Program Files" / name / "Program" / PROCESS_IMAGE,
                              base / "Program Files (x86)" / name / "Program" / PROCESS_IMAGE):
                if candidate.exists():
                    return str(candidate)
    return None


def workbench_window(pid: int) -> WinInfo | None:
    """The MDI child that hosts the project tree (title starts with 工作台)."""
    frame = main_frame(pid)
    if not frame:
        return None
    for child in child_windows(frame.hwnd):
        if child.visible and child.title.startswith("\u5de5\u4f5c\u53f0"):
            return child
    return None


def workbench_projects(pid: int) -> list[dict]:
    """Projects from the workbench window titles.

    The workbench title looks like ``工作台 : D:\\path\\project.MCP`` and gains a
    trailing ``*`` while the project has unsaved changes. Other windows (e.g.
    the toolbar) also contain the word 工作台, so the path must look like a real
    filesystem path.
    """
    projects: list[dict] = []
    frame = main_frame(pid)
    if not frame:
        return projects
    for child in child_windows(frame.hwnd):
        match = WORKBENCH_RE.match(child.title)
        if not match:
            continue
        raw, dirty = match.group(1).strip(), bool(match.group(2))
        if not looks_like_path(raw):
            continue
        if any(p["path"] == raw for p in projects):
            continue
        projects.append({"path": raw, "dirty": dirty})
    return projects


class ProjectLookupError(Exception):
    """A project could not be resolved; carries the list of valid choices."""

    def __init__(self, code: str, message: str, available: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.available = available or []


def all_projects() -> list[dict]:
    """Every open project across every running MCGS instance.

    MCGS may run more than one copy, and each copy's workbench reports the
    project it has open, so the full picture needs both loops.
    """
    found: list[dict] = []
    for proc in list_processes():
        for project in workbench_projects(proc["pid"]):
            found.append({
                "pid": proc["pid"],
                "path": project["path"],
                "dirty": project["dirty"],
                "name": Path(project["path"]).stem,
            })
    return found


def _normalise(text: str) -> str:
    return text.replace("/", "\\").strip().lower()


def resolve_project(project: str | None = None) -> tuple[int, str]:
    """Pick which MCGS instance and project a tool call should act on.

    With no argument the first project of the first process is used, which keeps
    the single-project workflow unchanged. Otherwise `project` may be an exact
    path, an exact project name, or a fragment that matches exactly one open
    project.
    """
    processes = list_processes()
    if not processes:
        raise ProjectLookupError("NO_MCGS_PROCESS", "没有正在运行的 McgsSetPro.exe。")

    projects = all_projects()
    if not projects:
        raise ProjectLookupError("NO_PROJECT_OPEN", "没有打开的工程。")

    if project is None or not str(project).strip():
        # Never guess. Process enumeration order is not stable, so "the first
        # project" can silently mean a different one after the user switches
        # windows. Callers must name their target.
        names = "、".join(p["name"] for p in projects)
        raise ProjectLookupError(
            "PROJECT_REQUIRED",
            f"必须用 project 指定工程。当前打开：{names}。",
            [{"name": p["name"], "path": p["path"]} for p in projects])

    wanted = _normalise(str(project))
    available = [{"name": p["name"], "path": p["path"]} for p in projects]

    exact_path = [p for p in projects if _normalise(p["path"]) == wanted]
    if exact_path:
        return exact_path[0]["pid"], exact_path[0]["path"]

    exact_name = [p for p in projects if p["name"].lower() == wanted]
    if len(exact_name) == 1:
        return exact_name[0]["pid"], exact_name[0]["path"]

    partial = [p for p in projects
               if wanted in _normalise(p["path"]) or wanted in p["name"].lower()]
    if len(partial) == 1:
        return partial[0]["pid"], partial[0]["path"]
    if len(partial) > 1:
        raise ProjectLookupError(
            "PROJECT_AMBIGUOUS",
            f"「{project}」匹配到 {len(partial)} 个工程，请写全名或完整路径。",
            [{"name": p["name"], "path": p["path"]} for p in partial])

    names = "、".join(p["name"] for p in projects)
    raise ProjectLookupError(
        "PROJECT_NOT_FOUND",
        f"没有名为「{project}」的已打开工程。当前打开：{names}。",
        available)


def canvas_window(pid: int) -> WinInfo | None:
    """The animation/configuration canvas, when a user window is open."""
    frame = main_frame(pid)
    if not frame:
        return None
    for child in child_windows(frame.hwnd):
        if child.cls == CANVAS_CLASS and child.visible:
            return child
    return None


# ---------------------------------------------------------------- dialogs
def modal_dialogs(pid: int) -> list[dict]:
    """Blocking dialogs, which is how MCGS reports a bad clipboard payload."""
    found = []
    for w in top_level_windows(pid):
        if w.cls != "#32770":
            continue
        texts = [c.title for c in child_windows(w.hwnd) if c.title]
        body = " ".join(texts)
        if not body:
            continue
        found.append({"hwnd": w.hwnd, "title": w.title, "text": body})
    return found

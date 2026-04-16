"""Find and focus the terminal window hosting a Claude Code process (Windows)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys

import psutil

# Windows terminal host process names (lowercase)
TERMINAL_HOSTS = {
    "windowsterminal.exe", "code.exe", "cmd.exe", "conhost.exe",
    "wezterm-gui.exe", "alacritty.exe", "hyper.exe", "tabby.exe",
    "wt.exe", "terminal.exe", "powershell.exe", "pwsh.exe",
    "mintty.exe", "iterm2", "ghostty.exe",
}


def _find_terminal_pid(claude_pid: int) -> int | None:
    """Walk up the process tree from claude.exe to find the terminal window host.

    Collects all ancestor PIDs that might own a visible window, then returns
    the one that actually has one.
    """
    try:
        proc = psutil.Process(claude_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    # collect ancestors up to 6 levels
    ancestors: list[int] = []
    current = proc
    for _ in range(6):
        parent = current.parent()
        if parent is None:
            break
        ancestors.append(parent.pid)
        current = parent

    # return the first ancestor that has a visible window
    for pid in ancestors:
        if _find_window_by_pid(pid) is not None:
            return pid

    return None


def _find_window_by_pid(target_pid: int) -> int | None:
    """Find the main visible window handle for a given PID."""
    if sys.platform != "win32":
        return None

    user32 = ctypes.windll.user32
    result: list[int] = []

    def enum_callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == target_pid:
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    result.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)

    return result[0] if result else None


def focus_terminal_window(claude_pid: int) -> bool:
    """Bring the terminal window hosting a Claude Code session to the foreground.

    Returns True if the window was found and focused.
    """
    if sys.platform != "win32":
        return False

    terminal_pid = _find_terminal_pid(claude_pid)
    if terminal_pid is None:
        return False

    hwnd = _find_window_by_pid(terminal_pid)
    if hwnd is None:
        return False

    user32 = ctypes.windll.user32

    # restore if minimized
    SW_RESTORE = 9
    SW_SHOW = 5
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    # SetForegroundWindow has restrictions: only works if the calling thread
    # is the foreground thread OR we use AllowSetForegroundWindow trick.
    # We use the AttachThreadInput trick to bypass this.
    foreground_hwnd = user32.GetForegroundWindow()
    foreground_tid = user32.GetWindowThreadProcessId(foreground_hwnd, None)
    current_tid = ctypes.windll.kernel32.GetCurrentThreadId()

    if foreground_tid != current_tid:
        user32.AttachThreadInput(current_tid, foreground_tid, True)

    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)

    if foreground_tid != current_tid:
        user32.AttachThreadInput(current_tid, foreground_tid, False)

    return True

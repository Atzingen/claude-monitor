"""Find and focus the terminal window hosting a Claude Code process (Windows)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import time

import psutil


def _get_shell_create_times() -> dict[int, float]:
    """Map shell PID -> creation time for all pwsh/bash shells under WindowsTerminal."""
    shells: dict[int, float] = {}
    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            name = proc.info["name"].lower()
            if name in ("pwsh.exe", "powershell.exe", "bash.exe", "zsh.exe", "cmd.exe"):
                parent = proc.parent()
                if parent and "terminal" in parent.name().lower():
                    shells[proc.info["pid"]] = proc.info["create_time"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return shells


def _find_tab_index(claude_pid: int) -> int | None:
    """Find the approximate tab index (1-based) for a Claude Code session.

    Maps claude PID -> parent shell PID -> creation order among all
    shells under WindowsTerminal. Tab order approximates creation order.
    """
    try:
        proc = psutil.Process(claude_pid)
        parent = proc.parent()
        if parent is None:
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    shell_pid = parent.pid
    shell_times = _get_shell_create_times()

    if shell_pid not in shell_times:
        return None

    # sort by creation time to get tab order
    sorted_pids = sorted(shell_times.keys(), key=lambda p: shell_times[p])
    try:
        return sorted_pids.index(shell_pid) + 1  # 1-based
    except ValueError:
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


def _find_terminal_ancestor(claude_pid: int) -> tuple[int | None, str]:
    """Find the terminal ancestor PID and its type ('wt', 'vscode', 'other')."""
    try:
        proc = psutil.Process(claude_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None, "unknown"

    current = proc
    for _ in range(6):
        parent = current.parent()
        if parent is None:
            break
        name = parent.name().lower()
        if "terminal" in name or name in ("wt.exe", "windowsterminal.exe"):
            return parent.pid, "wt"
        if "code" in name:
            # keep going up to find the main Code.exe with a window
            hwnd = _find_window_by_pid(parent.pid)
            if hwnd:
                return parent.pid, "vscode"
        current = parent

    # fallback: find first ancestor with a window
    current = proc
    for _ in range(6):
        parent = current.parent()
        if parent is None:
            break
        hwnd = _find_window_by_pid(parent.pid)
        if hwnd:
            return parent.pid, "other"
        current = parent

    return None, "unknown"


def _send_key_combo(vk_ctrl: bool, vk_alt: bool, vk_key: int) -> None:
    """Send a keyboard shortcut using SendInput."""
    if sys.platform != "win32":
        return

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_MENU = 0x12  # Alt

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.wintypes.WORD),
            ("wScan", ctypes.wintypes.WORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _fields_ = [
            ("type", ctypes.wintypes.DWORD),
            ("_input", _INPUT),
        ]

    def make_key_input(vk: int, up: bool = False) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp._input.ki.wVk = vk
        inp._input.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        return inp

    inputs = []
    if vk_ctrl:
        inputs.append(make_key_input(VK_CONTROL))
    if vk_alt:
        inputs.append(make_key_input(VK_MENU))
    inputs.append(make_key_input(vk_key))
    inputs.append(make_key_input(vk_key, up=True))
    if vk_alt:
        inputs.append(make_key_input(VK_MENU, up=True))
    if vk_ctrl:
        inputs.append(make_key_input(VK_CONTROL, up=True))

    arr = (INPUT * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def focus_terminal_window(claude_pid: int) -> tuple[bool, str]:
    """Bring the terminal window hosting a Claude Code session to the foreground.

    For Windows Terminal tabs, switches to the correct tab using Ctrl+Alt+<N>.

    Returns (success, message) tuple.
    """
    if sys.platform != "win32":
        return False, "Not supported on this platform"

    terminal_pid, terminal_type = _find_terminal_ancestor(claude_pid)
    if terminal_pid is None:
        return False, "Could not find terminal process"

    hwnd = _find_window_by_pid(terminal_pid)
    if hwnd is None:
        return False, "Could not find terminal window"

    user32 = ctypes.windll.user32

    # bring window to front
    SW_RESTORE = 9
    SW_SHOW = 5
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    foreground_hwnd = user32.GetForegroundWindow()
    foreground_tid = user32.GetWindowThreadProcessId(foreground_hwnd, None)
    current_tid = ctypes.windll.kernel32.GetCurrentThreadId()

    if foreground_tid != current_tid:
        user32.AttachThreadInput(current_tid, foreground_tid, True)

    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)

    if foreground_tid != current_tid:
        user32.AttachThreadInput(current_tid, foreground_tid, False)

    # for Windows Terminal: switch to the correct tab
    if terminal_type == "wt":
        tab_index = _find_tab_index(claude_pid)
        if tab_index is not None and 1 <= tab_index <= 9:
            time.sleep(0.1)  # let the window come to front first
            # Ctrl+Alt+<N> switches to tab N in Windows Terminal
            vk_number = 0x30 + tab_index  # VK_0 + N
            _send_key_combo(vk_ctrl=True, vk_alt=True, vk_key=vk_number)
            return True, f"Switched to tab {tab_index}"
        return True, "Window focused (tab index unknown)"

    if terminal_type == "vscode":
        return True, "Focused VSCode window"

    return True, "Window focused"

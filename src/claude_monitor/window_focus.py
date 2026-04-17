"""Find and focus the terminal window hosting a Claude Code process (Windows).

Strategy:
1. Find all WT windows (including other virtual desktops) via Win32 EnumWindows
2. Use UI Automation (pywinauto) to read tab titles from accessible windows
3. Match sessions to tabs by scanning tab titles for the session slug or cwd
4. Use IVirtualDesktopManager COM interface to detect which desktop each window is on
5. Focus the matched window and switch to its tab via Ctrl+Alt+N
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import time

import psutil


def _get_wt_windows() -> list[dict]:
    """Find all visible Windows Terminal windows with their hwnds and titles."""
    if sys.platform != "win32":
        return []

    user32 = ctypes.windll.user32
    wt_pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if "terminal" in proc.info["name"].lower():
                wt_pids.add(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not wt_pids:
        return []

    windows: list[dict] = []

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in wt_pids:
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                windows.append({"hwnd": hwnd, "pid": pid.value, "title": buf.value})
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return windows


def _get_desktop_info(hwnd: int) -> tuple[bool, int]:
    """Check if window is on current virtual desktop and get desktop number.

    Returns (is_current_desktop, desktop_number).
    """
    try:
        import comtypes

        class IVirtualDesktopManager(comtypes.IUnknown):
            _iid_ = comtypes.GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")
            _methods_ = [
                comtypes.COMMETHOD([], ctypes.HRESULT, "IsWindowOnCurrentVirtualDesktop",
                    (["in"], ctypes.wintypes.HWND, "w"),
                    (["out"], ctypes.POINTER(ctypes.c_int), "r")),
                comtypes.COMMETHOD([], ctypes.HRESULT, "GetWindowDesktopId",
                    (["in"], ctypes.wintypes.HWND, "w"),
                    (["out"], ctypes.POINTER(comtypes.GUID), "r")),
            ]

        vdm = comtypes.CoCreateInstance(
            comtypes.GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}"),
            interface=IVirtualDesktopManager,
        )
        is_current = bool(vdm.IsWindowOnCurrentVirtualDesktop(hwnd))
        return is_current, 0
    except Exception:
        return True, 0


def _get_tab_titles(hwnd: int) -> list[str]:
    """Use UI Automation to read tab titles from a WT window."""
    try:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        for win in desktop.windows(visible_only=False):
            if win.handle == hwnd:
                tabs = win.descendants(control_type="TabItem")
                return [t.window_text() for t in tabs]
    except Exception:
        pass
    return []


def _find_vscode_window(claude_pid: int) -> int | None:
    """Find the VSCode window for a Claude Code session running in VSCode."""
    user32 = ctypes.windll.user32
    try:
        proc = psutil.Process(claude_pid)
        current = proc
        for _ in range(6):
            parent = current.parent()
            if parent is None:
                break
            if "code" in parent.name().lower():
                # find window for this or ancestor Code.exe
                check = parent
                for _ in range(3):
                    result = []
                    def cb(hwnd, _):
                        if user32.IsWindowVisible(hwnd):
                            pid = ctypes.wintypes.DWORD()
                            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                            if pid.value == check.pid:
                                length = user32.GetWindowTextLengthW(hwnd)
                                if length > 10:
                                    result.append(hwnd)
                        return True
                    WNDENUMPROC = ctypes.WINFUNCTYPE(
                        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
                    )
                    user32.EnumWindows(WNDENUMPROC(cb), 0)
                    if result:
                        return result[0]
                    p = check.parent()
                    if p and "code" in p.name().lower():
                        check = p
                    else:
                        break
                return None
            current = parent
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def _is_vscode_session(claude_pid: int) -> bool:
    """Check if this Claude Code session runs inside VSCode."""
    try:
        proc = psutil.Process(claude_pid)
        current = proc
        for _ in range(4):
            parent = current.parent()
            if parent is None:
                break
            if "code" in parent.name().lower():
                return True
            current = parent
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return False


def _bring_window_to_front(hwnd: int) -> bool:
    """Bring a window to the foreground, even across virtual desktops."""
    user32 = ctypes.windll.user32

    SW_RESTORE = 9
    SW_SHOW = 5
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    # SwitchToThisWindow works across virtual desktops
    user32.SwitchToThisWindow(hwnd, True)
    return True


def _send_ctrl_alt_n(n: int) -> None:
    """Send Ctrl+Alt+N keystroke to switch WT tab."""
    if n < 1 or n > 9:
        return

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    vk_key = 0x30 + n

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

    def key(vk: int, up: bool = False) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp._input.ki.wVk = vk
        inp._input.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        return inp

    inputs = [
        key(VK_CONTROL), key(VK_MENU), key(vk_key),
        key(vk_key, True), key(VK_MENU, True), key(VK_CONTROL, True),
    ]
    arr = (INPUT * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _get_shell_to_window_map() -> dict[int, int]:
    """Map shell PIDs to WT window hwnds by creation time ordering.

    WT creates shells in order across windows. We map each shell to the
    WT window by distributing shells across windows based on the number
    of tabs visible in each window.
    """
    # This is a heuristic - not 100% accurate
    return {}


def get_session_location(claude_pid: int, slug: str = "", cwd: str = "") -> dict:
    """Get the terminal location info for a Claude Code session.

    Returns dict with: hwnd, tab_index, tab_title, is_current_desktop,
    window_title, host_type ('wt' or 'vscode'), matched_by.
    """
    result = {
        "hwnd": None,
        "tab_index": None,
        "tab_title": None,
        "is_current_desktop": None,
        "window_title": None,
        "host_type": None,
        "matched_by": None,
    }

    if sys.platform != "win32":
        return result

    # Check if VSCode session
    if _is_vscode_session(claude_pid):
        hwnd = _find_vscode_window(claude_pid)
        if hwnd:
            is_current, _ = _get_desktop_info(hwnd)
            user32 = ctypes.windll.user32
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            result.update({
                "hwnd": hwnd,
                "is_current_desktop": is_current,
                "window_title": buf.value,
                "host_type": "vscode",
                "matched_by": "process_tree",
            })
        return result

    # Windows Terminal: try tab title matching first
    wt_windows = _get_wt_windows()
    if not wt_windows:
        return result

    cwd_name = cwd.replace("\\", "/").rstrip("/").split("/")[-1].lower() if cwd else ""

    for w in wt_windows:
        tabs = _get_tab_titles(w["hwnd"])
        is_current, _ = _get_desktop_info(w["hwnd"])

        for i, tab_title in enumerate(tabs):
            tab_lower = tab_title.lower()
            # match by slug, cwd folder name, or partial text
            if slug and slug.lower() in tab_lower:
                result.update(hwnd=w["hwnd"], tab_index=i+1, tab_title=tab_title,
                              is_current_desktop=is_current, window_title=w["title"],
                              host_type="wt", matched_by="slug")
                return result
            if cwd_name and len(cwd_name) > 3 and cwd_name in tab_lower:
                result.update(hwnd=w["hwnd"], tab_index=i+1, tab_title=tab_title,
                              is_current_desktop=is_current, window_title=w["title"],
                              host_type="wt", matched_by="cwd")
                return result

    # no exact match - return first WT window as fallback
    w = wt_windows[0]
    is_current, _ = _get_desktop_info(w["hwnd"])
    result.update(
        hwnd=w["hwnd"],
        is_current_desktop=is_current,
        window_title=w["title"],
        host_type="wt",
        matched_by="fallback",
    )
    return result


def focus_terminal_window(claude_pid: int, slug: str = "", cwd: str = "") -> tuple[bool, str]:
    """Focus the terminal window for a Claude Code session.

    Returns (success, message) with info about what was done.
    """
    if sys.platform != "win32":
        return False, "Not supported on this platform"

    loc = get_session_location(claude_pid, slug, cwd)

    if loc["hwnd"] is None:
        return False, "Could not find terminal window"

    _bring_window_to_front(loc["hwnd"])

    parts = []
    if loc["host_type"] == "vscode":
        parts.append("Focused VSCode")
    elif loc["host_type"] == "wt":
        if loc["tab_index"] and 1 <= loc["tab_index"] <= 9:
            time.sleep(0.15)
            _send_ctrl_alt_n(loc["tab_index"])
            parts.append(f"Tab {loc['tab_index']}")
        if loc["matched_by"] == "fallback":
            parts.append("(window found, tab unknown)")
        elif loc.get("tab_title"):
            safe = loc["tab_title"].encode("ascii", errors="replace").decode()[:30]
            parts.append(f'"{safe}"')

    if loc.get("is_current_desktop") is False:
        parts.append("(switched desktop)")

    return True, " | ".join(parts) if parts else "Focused"

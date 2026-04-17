"""Find and focus the terminal window hosting a Claude Code process (Windows).

Uses PowerShell for reliable window management across virtual desktops.
"""

from __future__ import annotations

import json
import subprocess
import sys

import psutil


# PowerShell script that enumerates windows, finds the right one, and focuses it.
# Accepts: -TargetPid (claude.exe PID) -Action (list|focus)
PS_SCRIPT = r'''
param([int]$TargetPid, [string]$Action = "focus")

Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class WinHelper {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lp);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] public static extern bool SwitchToThisWindow(IntPtr hWnd, bool fAlt);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);

    public static List<IntPtr> FindWindowsByPid(uint pid) {
        var r = new List<IntPtr>();
        EnumWindows((h, _) => {
            if (IsWindowVisible(h)) { uint p; GetWindowThreadProcessId(h, out p);
                if (p == pid && GetWindowTextLength(h) > 0) r.Add(h); }
            return true;
        }, IntPtr.Zero);
        return r;
    }
    public static string GetTitle(IntPtr h) {
        var sb = new StringBuilder(GetWindowTextLength(h) + 1);
        GetWindowText(h, sb, sb.Capacity); return sb.ToString();
    }
    public static void Focus(IntPtr h) {
        if (IsIconic(h)) { ShowWindow(h, 9); }
        SwitchToThisWindow(h, true);
        SetForegroundWindow(h);
    }
}
"@

# Walk process tree: claude.exe -> shell -> WindowsTerminal or Code.exe
$proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
if (-not $proc) { Write-Output '{"error":"process not found"}'; exit 1 }

# Walk up to find terminal host
$current = $proc
$hostPid = 0
$hostType = "unknown"
for ($i = 0; $i -lt 6; $i++) {
    $parentPid = (Get-CimInstance Win32_Process -Filter "ProcessId=$($current.Id)" -ErrorAction SilentlyContinue).ParentProcessId
    if (-not $parentPid) { break }
    $parent = Get-Process -Id $parentPid -ErrorAction SilentlyContinue
    if (-not $parent) { break }
    $name = $parent.ProcessName.ToLower()
    if ($name -match "terminal") { $hostPid = $parent.Id; $hostType = "wt"; break }
    if ($name -match "code") { $hostPid = $parent.Id; $hostType = "vscode"; break }
    $current = $parent
}

if ($hostPid -eq 0) { Write-Output '{"error":"no terminal host found"}'; exit 1 }

# Find all windows for the host process
$handles = [WinHelper]::FindWindowsByPid([uint32]$hostPid)

if ($handles.Count -eq 0) {
    # For VSCode, the window might be owned by a parent process
    if ($hostType -eq "vscode") {
        $vscParent = (Get-CimInstance Win32_Process -Filter "ProcessId=$hostPid" -ErrorAction SilentlyContinue).ParentProcessId
        if ($vscParent) { $handles = [WinHelper]::FindWindowsByPid([uint32]$vscParent) }
    }
}

if ($handles.Count -eq 0) { Write-Output '{"error":"no windows found"}'; exit 1 }

# For single window (VSCode) or if only 1 WT window, just focus it
if ($handles.Count -eq 1 -or $hostType -eq "vscode") {
    $h = $handles[0]
    $title = [WinHelper]::GetTitle($h)
    if ($Action -eq "focus") { [WinHelper]::Focus($h) }
    Write-Output "{`"ok`":true,`"hwnd`":$($h.ToInt64()),`"title`":`"$($title -replace '"','\"')`",`"type`":`"$hostType`",`"windows`":1}"
    exit 0
}

# Multiple WT windows: try to find which one has the right tab
# Strategy: focus each window briefly to read its tab titles via the window title
# The window title shows the active tab's title
$result = @()
foreach ($h in $handles) {
    $title = [WinHelper]::GetTitle($h)
    $result += @{ hwnd = $h.ToInt64(); title = $title }
}

# Output all windows as JSON for the Python side to decide
$json = $result | ConvertTo-Json -Compress
Write-Output "{`"ok`":true,`"type`":`"wt`",`"windows`":$($handles.Count),`"list`":$json}"

# If action is focus, focus the first window as default
if ($Action -eq "focus") {
    [WinHelper]::Focus($handles[0])
}
'''


def _run_ps(claude_pid: int, action: str = "focus") -> dict:
    """Run the PowerShell helper and return parsed JSON result."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", PS_SCRIPT],
            input=None,
            capture_output=True,
            text=True,
            timeout=10,
            env={"TargetPid": str(claude_pid), "Action": action,
                 "SystemRoot": "C:\\Windows", "PATH": "C:\\Windows\\System32"},
        )
        # PS params via env don't work, use -Command with args
    except Exception as e:
        return {"error": str(e)}

    # Actually, pass params directly in the command
    return {}


def _run_ps_focus(claude_pid: int) -> dict:
    """Run PowerShell to find and focus the terminal window."""
    cmd = PS_SCRIPT.replace("param([int]$TargetPid, [string]$Action = \"focus\")",
                             f"$TargetPid = {claude_pid}; $Action = 'focus'")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip().split("\n")[-1])
        if result.stderr.strip():
            return {"error": result.stderr.strip()[:200]}
        return {"error": "no output"}
    except json.JSONDecodeError:
        return {"error": f"parse error: {result.stdout.strip()[:100]}"}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}


def _run_ps_focus_hwnd(hwnd: int) -> bool:
    """Focus a specific window by hwnd via PowerShell."""
    cmd = f"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {{
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool SwitchToThisWindow(IntPtr h, bool f);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
}}
"@
$h = [IntPtr]{hwnd}
if ([W]::IsIconic($h)) {{ [W]::ShowWindow($h, 9) }}
[W]::SwitchToThisWindow($h, $true)
[W]::SetForegroundWindow($h)
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, timeout=5,
        )
        return True
    except Exception:
        return False


def focus_terminal_window(claude_pid: int, slug: str = "", cwd: str = "") -> tuple[bool, str]:
    """Focus the terminal window for a Claude Code session.

    Uses PowerShell for reliable cross-desktop window management.
    Returns (success, message).
    """
    if sys.platform != "win32":
        return False, "Not supported on this platform"

    result = _run_ps_focus(claude_pid)

    if "error" in result:
        return False, result["error"]

    if not result.get("ok"):
        return False, "Unknown error"

    host_type = result.get("type", "?")
    num_windows = result.get("windows", 0)

    if host_type == "vscode":
        title = result.get("title", "")
        safe_title = title.encode("ascii", errors="replace").decode()[:40]
        return True, f"Focused VSCode: {safe_title}"

    if num_windows == 1:
        title = result.get("title", "")
        safe_title = title.encode("ascii", errors="replace").decode()[:40]
        return True, f"Focused: {safe_title}"

    # Multiple WT windows - the PS script focused the first one
    # Try to find the right one by matching cwd/slug in window titles
    window_list = result.get("list", [])
    if isinstance(window_list, dict):
        window_list = [window_list]

    cwd_name = cwd.replace("\\", "/").rstrip("/").split("/")[-1].lower() if cwd else ""

    for w in window_list:
        title = w.get("title", "").lower()
        if slug and slug.lower() in title:
            _run_ps_focus_hwnd(w["hwnd"])
            safe = w.get("title", "").encode("ascii", errors="replace").decode()[:40]
            return True, f"Focused: {safe} (matched slug)"
        if cwd_name and len(cwd_name) > 3 and cwd_name in title:
            _run_ps_focus_hwnd(w["hwnd"])
            safe = w.get("title", "").encode("ascii", errors="replace").decode()[:40]
            return True, f"Focused: {safe} (matched cwd)"

    # No exact match - show available windows
    titles = [w.get("title", "?").encode("ascii", errors="replace").decode()[:30] for w in window_list]
    return True, f"Focused 1 of {num_windows} windows. Others: {', '.join(titles[1:])}"


def get_session_location(claude_pid: int, slug: str = "", cwd: str = "") -> dict:
    """Stub for compatibility - not used in current implementation."""
    return {}

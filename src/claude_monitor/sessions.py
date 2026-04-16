"""Discover and track Claude Code sessions from ~/.claude/ metadata."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil


CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"


@dataclass
class ClaudeSession:
    pid: int
    session_id: str
    cwd: str
    started_at: float  # epoch ms
    kind: str
    entrypoint: str
    is_alive: bool = False
    # enriched from sessions-index
    summary: str = ""
    first_prompt: str = ""
    message_count: int = 0
    git_branch: str = ""
    project_name: str = ""
    last_modified: str = ""

    @property
    def started_at_str(self) -> str:
        t = time.localtime(self.started_at / 1000)
        return time.strftime("%Y-%m-%d %H:%M", t)

    @property
    def runtime_str(self) -> str:
        if not self.is_alive:
            return "stopped"
        elapsed = time.time() - (self.started_at / 1000)
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        if minutes > 0:
            return f"{minutes}m{seconds:02d}s"
        return f"{seconds}s"

    @property
    def display_name(self) -> str:
        if self.project_name:
            return self.project_name
        # extract last folder from cwd
        return Path(self.cwd).name if self.cwd else "unknown"

    @property
    def status_icon(self) -> str:
        return "[green]RUNNING[/]" if self.is_alive else "[dim]STOPPED[/]"


def _get_alive_pids() -> set[int]:
    """Return set of PIDs for running claude.exe processes."""
    alive = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info["name"].lower()
            if "claude" in name:
                alive.add(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return alive


def _load_session_files() -> list[dict]:
    """Read all ~/.claude/sessions/*.json files."""
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sessions.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return sessions


def _build_session_index() -> dict[str, dict]:
    """Build a lookup from sessionId -> index entry across all projects."""
    index: dict[str, dict] = {}
    if not PROJECTS_DIR.exists():
        return index
    for idx_file in PROJECTS_DIR.glob("*/sessions-index.json"):
        try:
            data = json.loads(idx_file.read_text(encoding="utf-8"))
            for entry in data.get("entries", []):
                sid = entry.get("sessionId", "")
                if sid:
                    # derive project name from directory
                    project_dir = idx_file.parent.name
                    entry["_project_dir"] = project_dir
                    index[sid] = entry
        except (json.JSONDecodeError, OSError):
            continue
    return index


def _project_dir_to_name(project_dir: str) -> str:
    """Convert 'C--Users-Gustavo-Desktop-dev-alimentacidades' to 'alimentacidades'."""
    parts = project_dir.replace("C--", "").split("-")
    # find the last meaningful segment(s)
    # typical: Users-Gustavo-Desktop-dev-projectname
    # we want the part after 'dev-' or the last segment
    full = project_dir.replace("C--", "").replace("-", "/")
    return Path(full).name


def discover_sessions() -> list[ClaudeSession]:
    """Discover all Claude Code sessions, both alive and recent."""
    alive_pids = _get_alive_pids()
    raw_sessions = _load_session_files()
    session_index = _build_session_index()

    sessions: list[ClaudeSession] = []
    seen_ids: set[str] = set()

    for raw in raw_sessions:
        pid = raw.get("pid", 0)
        session_id = raw.get("sessionId", "")
        if session_id in seen_ids:
            continue
        seen_ids.add(session_id)

        session = ClaudeSession(
            pid=pid,
            session_id=session_id,
            cwd=raw.get("cwd", ""),
            started_at=raw.get("startedAt", 0),
            kind=raw.get("kind", ""),
            entrypoint=raw.get("entrypoint", ""),
            is_alive=pid in alive_pids,
        )

        # enrich from index
        idx_entry = session_index.get(session_id, {})
        if idx_entry:
            session.summary = idx_entry.get("summary", "")
            session.first_prompt = idx_entry.get("firstPrompt", "")
            session.message_count = idx_entry.get("messageCount", 0)
            session.git_branch = idx_entry.get("gitBranch", "")
            session.last_modified = idx_entry.get("modified", "")
            project_dir = idx_entry.get("_project_dir", "")
            if project_dir:
                session.project_name = _project_dir_to_name(project_dir)

        sessions.append(session)

    # sort: alive first, then by started_at descending
    sessions.sort(key=lambda s: (not s.is_alive, -s.started_at))
    return sessions

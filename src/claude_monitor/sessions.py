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
class ConversationMessage:
    role: str  # "user" or "assistant"
    text: str
    timestamp: str
    msg_type: str  # "text", "tool_use", "tool_result"


@dataclass
class ClaudeSession:
    pid: int
    session_id: str
    cwd: str
    started_at: float  # epoch ms
    kind: str
    entrypoint: str
    is_alive: bool = False
    # enriched from JSONL / index
    summary: str = ""
    first_prompt: str = ""
    message_count: int = 0
    git_branch: str = ""
    project_name: str = ""
    last_modified: str = ""
    slug: str = ""
    # state detection
    last_message_role: str = ""  # "user" or "assistant"
    last_message_time: str = ""
    jsonl_path: Path | None = None

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
        return Path(self.cwd).name if self.cwd else "unknown"

    @property
    def activity_status(self) -> str:
        """Detect if Claude is processing or waiting for user input."""
        if not self.is_alive:
            return "stopped"
        if not self.last_message_role:
            return "unknown"
        if self.last_message_role == "user":
            return "processing"
        return "waiting"

    @property
    def activity_display(self) -> str:
        status = self.activity_status
        if status == "processing":
            return "[bold yellow]\u25cf PROCESSING[/]"
        if status == "waiting":
            return "[bold green]\u25cf WAITING[/]"
        if status == "stopped":
            return "[dim]\u25cf STOPPED[/]"
        return "[dim]\u25cb ...[/]"


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


def _find_jsonl(session_id: str) -> Path | None:
    """Find the JSONL conversation file for a session across all projects."""
    if not PROJECTS_DIR.exists():
        return None
    matches = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _read_jsonl_tail(jsonl_path: Path, max_lines: int = 50) -> list[dict]:
    """Read the last N lines from a JSONL file efficiently."""
    try:
        with open(jsonl_path, "rb") as f:
            # seek to end and read backwards to find last N lines
            f.seek(0, 2)
            file_size = f.tell()
            if file_size == 0:
                return []

            # read last chunk (generous size to get enough lines)
            chunk_size = min(file_size, max_lines * 2000)
            f.seek(file_size - chunk_size)
            data = f.read().decode("utf-8", errors="replace")

        lines = data.strip().split("\n")
        # take last max_lines
        lines = lines[-max_lines:]

        result = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
    except OSError:
        return []


def _enrich_from_jsonl(session: ClaudeSession) -> None:
    """Enrich session with data from its JSONL conversation file."""
    jsonl_path = _find_jsonl(session.session_id)
    if jsonl_path is None:
        return

    session.jsonl_path = jsonl_path

    # derive project name from cwd (more accurate than dir encoding)
    if not session.project_name:
        session.project_name = _project_dir_to_name(jsonl_path.parent.name, session.cwd)

    # read tail to get state + count messages
    entries = _read_jsonl_tail(jsonl_path, max_lines=200)

    msg_count = 0
    last_role = ""
    last_time = ""
    first_user_text = ""
    slug = ""

    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type in ("assistant", "user"):
            msg_count += 1
            last_role = entry_type
            last_time = entry.get("timestamp", "")

        if not slug:
            slug = entry.get("slug", "")

        # get first user prompt
        if entry_type == "user" and not first_user_text:
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            first_user_text = block.get("text", "")
                            break
                elif isinstance(content, str):
                    first_user_text = content

    # also do a rough full count by reading file line count
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            total_lines = sum(1 for _ in f)
        # approximate: about 60% of lines are user/assistant messages
        # but we'll use what we counted from the tail as a minimum
        session.message_count = max(msg_count, _count_messages_fast(jsonl_path))
    except OSError:
        session.message_count = msg_count

    session.last_message_role = last_role
    session.last_message_time = last_time
    session.slug = slug
    if first_user_text and not session.first_prompt:
        session.first_prompt = first_user_text


def _count_messages_fast(jsonl_path: Path) -> int:
    """Count user+assistant messages by scanning the file for type fields."""
    count = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                # fast check without full JSON parse
                if '"type":"assistant"' in line or '"type": "assistant"' in line:
                    count += 1
                elif '"type":"user"' in line or '"type": "user"' in line:
                    count += 1
    except OSError:
        pass
    return count


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
                    project_dir = idx_file.parent.name
                    entry["_project_dir"] = project_dir
                    index[sid] = entry
        except (json.JSONDecodeError, OSError):
            continue
    return index


def _project_dir_to_name(project_dir: str, cwd: str = "") -> str:
    """Derive a project name, preferring the actual cwd path."""
    if cwd:
        return Path(cwd).name
    # fallback: strip the common prefix from the dir encoding
    full = project_dir.replace("C--", "").replace("c--", "").replace("-", "/")
    return Path(full).name


def get_conversation_text(session: ClaudeSession, max_messages: int = 50) -> list[ConversationMessage]:
    """Read the conversation messages from a session's JSONL file."""
    if session.jsonl_path is None:
        jsonl_path = _find_jsonl(session.session_id)
        if jsonl_path is None:
            return []
    else:
        jsonl_path = session.jsonl_path

    entries = _read_jsonl_tail(jsonl_path, max_lines=max_messages * 3)

    messages: list[ConversationMessage] = []
    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type not in ("assistant", "user"):
            continue

        timestamp = entry.get("timestamp", "")
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", entry_type)
        content = msg.get("content", "")
        text_parts: list[str] = []
        msg_type = "text"

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    msg_type = "tool_use"
                    text_parts.append(f"[tool: {tool_name}]")
                elif block_type == "tool_result":
                    msg_type = "tool_result"
                    # skip verbose tool results
                    continue
        elif isinstance(content, str):
            text_parts.append(content)

        text = "\n".join(text_parts).strip()
        if not text:
            continue

        messages.append(ConversationMessage(
            role=role,
            text=text,
            timestamp=timestamp,
            msg_type=msg_type,
        ))

    # return last max_messages
    return messages[-max_messages:]


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

        # enrich from index first
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

        # always enrich from JSONL (overrides index for active sessions)
        _enrich_from_jsonl(session)

        sessions.append(session)

    # sort: alive first, then by started_at descending
    sessions.sort(key=lambda s: (not s.is_alive, -s.started_at))
    return sessions

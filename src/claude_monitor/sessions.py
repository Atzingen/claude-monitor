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
    last_prompt: str = ""
    # state detection
    last_message_role: str = ""  # "user" or "assistant"
    last_message_time: str = ""
    jsonl_path: Path | None = None
    # token usage
    model: str = ""
    context_tokens: int = 0  # cache_read (current context size)
    input_tokens: int = 0  # new + cache_creation
    output_tokens: int = 0  # generated tokens
    # real-time activity
    cpu_percent: float = 0.0
    jsonl_mtime_age: float = 999.0  # seconds since last JSONL write

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
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def context_limit(self) -> int:
        """Max context window for the model."""
        model = self.model.lower()
        if "opus" in model:
            return 1_000_000
        if "sonnet" in model:
            return 200_000
        if "haiku" in model:
            return 200_000
        return 200_000  # safe default

    @property
    def context_pct(self) -> float:
        if self.context_tokens == 0:
            return 0.0
        return (self.context_tokens / self.context_limit) * 100

    @property
    def context_display(self) -> str:
        """Format context as percentage + absolute."""
        if self.context_tokens == 0:
            return "-"
        pct = self.context_pct
        if self.context_tokens >= 1_000_000:
            abs_str = f"{self.context_tokens / 1_000_000:.1f}M"
        elif self.context_tokens >= 1_000:
            abs_str = f"{self.context_tokens / 1_000:.0f}k"
        else:
            abs_str = str(self.context_tokens)
        return f"{pct:.0f}% ({abs_str})"

    @property
    def activity_status(self) -> str:
        """Detect session state using CPU + JSONL mtime + last message role."""
        if not self.is_alive:
            return "stopped"
        # actively writing to JSONL or using CPU = definitely processing
        if self.jsonl_mtime_age < 30 or self.cpu_percent > 5:
            return "processing"
        # no CPU, old mtime = idle/waiting regardless of last role
        if self.last_message_role == "assistant":
            return "waiting"
        # last_message_role == "user" but no activity = stale, treat as waiting
        if self.jsonl_mtime_age > 60 and self.cpu_percent < 1:
            return "waiting"
        return "idle"

    @property
    def activity_display(self) -> str:
        status = self.activity_status
        if status == "processing":
            return "[bold yellow]\u25cf PROCESSING[/]"
        if status == "waiting":
            return "[bold green]\u25cf WAITING[/]"
        if status == "idle":
            return "[dim green]\u25cf IDLE[/]"
        if status == "stopped":
            return "[dim]\u25cf STOPPED[/]"
        return "[dim]\u25cb ...[/]"


def _get_alive_pids() -> dict[int, float]:
    """Return dict of PID -> cpu_percent for running claude.exe processes."""
    alive: dict[int, float] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = proc.info["name"].lower()
            if "claude" in name:
                cpu = proc.cpu_percent(interval=0)
                alive[proc.info["pid"]] = cpu
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # second pass for more accurate CPU reading
    if alive:
        time.sleep(0.1)
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["pid"] in alive:
                    alive[proc.info["pid"]] = proc.cpu_percent(interval=0)
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


def _extract_user_text(entry: dict) -> str:
    """Extract text content from a user message entry."""
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    elif isinstance(content, str):
        return content
    return ""


def _enrich_from_jsonl(session: ClaudeSession) -> None:
    """Enrich session with data from its JSONL conversation file."""
    jsonl_path = _find_jsonl(session.session_id)
    if jsonl_path is None:
        return

    session.jsonl_path = jsonl_path
    session.jsonl_mtime_age = time.time() - jsonl_path.stat().st_mtime

    # derive project name from cwd (more accurate than dir encoding)
    if not session.project_name:
        session.project_name = _project_dir_to_name(jsonl_path.parent.name, session.cwd)

    # read tail to get state + recent context
    entries = _read_jsonl_tail(jsonl_path, max_lines=200)

    msg_count = 0
    last_role = ""
    last_time = ""
    first_user_text = ""
    last_user_text = ""
    slug = ""
    last_context_tokens = 0
    last_input_tokens = 0
    last_output_tokens = 0
    model = ""

    for entry in entries:
        entry_type = entry.get("type", "")
        if entry_type in ("assistant", "user"):
            msg_count += 1
            last_role = entry_type
            last_time = entry.get("timestamp", "")

        if not slug:
            slug = entry.get("slug", "")

        # extract user prompts
        if entry_type == "user":
            text = _extract_user_text(entry)
            if text:
                if not first_user_text:
                    first_user_text = text
                last_user_text = text

        # extract token usage from last assistant message
        if entry_type == "assistant":
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                if msg.get("model"):
                    model = msg["model"]
                usage = msg.get("usage", {})
                if usage:
                    last_context_tokens = usage.get("cache_read_input_tokens", 0)
                    last_input_tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                    )
                    last_output_tokens = usage.get("output_tokens", 0)

    # full message count from file scan
    session.message_count = max(msg_count, _count_and_sum_tokens(jsonl_path, session))

    session.last_message_role = last_role
    session.last_message_time = last_time
    session.slug = slug
    session.context_tokens = last_context_tokens
    if model:
        session.model = model

    if first_user_text and not session.first_prompt:
        session.first_prompt = first_user_text
    if last_user_text:
        session.last_prompt = last_user_text


def _count_and_sum_tokens(jsonl_path: Path, session: ClaudeSession) -> int:
    """Count messages and sum total token usage across the full session."""
    count = 0
    total_input = 0
    total_output = 0
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                if '"type": "assistant"' in line or '"type":"assistant"' in line:
                    count += 1
                    # parse for token usage
                    try:
                        data = json.loads(line)
                        msg = data.get("message", {})
                        if isinstance(msg, dict):
                            usage = msg.get("usage", {})
                            if usage:
                                total_input += (
                                    usage.get("input_tokens", 0)
                                    + usage.get("cache_creation_input_tokens", 0)
                                )
                                total_output += usage.get("output_tokens", 0)
                    except json.JSONDecodeError:
                        pass
                elif '"type": "user"' in line or '"type":"user"' in line:
                    count += 1
    except OSError:
        pass

    session.input_tokens = total_input
    session.output_tokens = total_output
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
            cpu_percent=alive_pids.get(pid, 0.0),
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

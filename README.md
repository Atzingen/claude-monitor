# claude-code-monitor

A terminal dashboard (TUI) to monitor and interact with all your running [Claude Code](https://claude.ai/code) instances from a single screen.

Built with [Textual](https://textual.textualize.io/) for Python.

## Why?

If you run multiple Claude Code sessions across different terminals, tabs, and virtual desktops, it's hard to keep track of what each one is doing. `claude-code-monitor` gives you a single dashboard where you can:

- See which sessions are **actively processing** vs **waiting for input**
- Read the **conversation history** of any session without switching terminals
- Track **context window usage** (% of model limit) per session
- Monitor **token consumption** across all sessions
- Send messages to sessions directly from the dashboard

## Installation

```bash
pip install claude-code-monitor
```

That's it. Run it with:

```bash
cm
```

Or the full name:

```bash
claude-code-monitor
```

### Requirements

- Python 3.10+
- Claude Code CLI installed and running (`claude` command available in PATH)
- Works on **Windows**, **Linux**, and **macOS**

## Features

### Live Session Dashboard

The left panel shows all Claude Code sessions with real-time status:

| Column   | Description |
|----------|-------------|
| Status   | `PROCESSING` (animated spinner) / `WAITING` (idle) / `STOPPED` |
| Project  | Working directory name |
| PID      | Process ID |
| Runtime  | Time since session started |
| Msgs     | Total messages in the conversation |
| Context  | Context window usage as % of model limit |

### Session Details

The right panel shows details of the selected session:

- PID, session ID, slug, working directory
- Runtime, message count, context usage
- Token consumption (input/output)
- Git branch (when available)
- First and last prompts sent

### Conversation Viewer

See what each Claude Code instance is actually doing:

- **Text messages** from you (USER) and Claude (CLAUDE)
- **Tool calls** with human-readable summaries:
  - `Edit views.py` instead of raw JSON
  - `$ Run tests locally` (Bash descriptions)
  - `Task -> completed` (task status changes)
  - `Grep "pattern"`, `Write file.py`, etc.
- Auto-refreshes every 3 seconds to show new activity

### Usage Metrics

Bottom bar shows aggregate stats:

- **Weekly / Session usage %** (requires [ccstatusline](https://github.com/sirmalloc/ccstatusline))
- **Average and max context window** across all active sessions

### Send Messages

Send prompts to any session directly from the dashboard using the `s` key. The response appears in the conversation viewer.

## Keybindings

| Key     | Action |
|---------|--------|
| Up/Down | Navigate between sessions |
| Enter   | Focus conversation view (scrollable) |
| s       | Send a message to the selected session |
| r       | Manual refresh |
| ESC     | Back to session list |
| q       | Quit |

## How It Works

`claude-code-monitor` reads Claude Code's local session metadata:

- `~/.claude/sessions/*.json` - active session PIDs and metadata
- `~/.claude/projects/*/sessions-index.json` - session summaries
- `~/.claude/projects/*/<sessionId>.jsonl` - full conversation history
- `~/.cache/ccstatusline/usage.json` - usage % (optional, from ccstatusline)

It cross-references session PIDs with running processes to determine which sessions are alive, and uses CPU usage + JSONL write timestamps to detect whether Claude is actively processing or waiting for input.

No API calls are made. Everything is read from local files.

## Optional: Usage % Display

To show `Weekly: N% | Session: N%` in the dashboard, install [ccstatusline](https://github.com/sirmalloc/ccstatusline) as a Claude Code status line:

```bash
claude config set statusLine '{"type":"command","command":"npx -y ccstatusline@latest","padding":0}'
```

Without it, the usage percentages will show as 0%.

## License

MIT

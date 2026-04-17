"""Claude Code Monitor - TUI dashboard for managing Claude Code instances."""

from __future__ import annotations

import subprocess
import sys

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)

from claude_monitor.sessions import (
    ClaudeSession,
    ConversationMessage,
    discover_sessions,
    get_conversation_text,
    get_usage_info,
)
from claude_monitor.window_focus import focus_terminal_window


REFRESH_INTERVAL = 5.0
CONVERSATION_REFRESH_INTERVAL = 3.0

SPINNER_FRAMES = ["\u280b", "\u2819", "\u2838", "\u2830", "\u2826", "\u2807"]  # braille spinner


def make_status_text(status: str, frame: int = 0) -> Text:
    if status == "processing":
        spinner = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
        return Text(f"{spinner} PROCESSING", style="bold yellow")
    if status == "waiting":
        return Text("\u25cf WAITING", style="bold green")
    if status == "idle":
        return Text("\u25cb IDLE", style="dim green")
    if status == "stopped":
        return Text("\u25cf stopped", style="dim")
    return Text("\u25cb ...", style="dim")


class SessionDetailPanel(Static):
    """Shows details of the selected session."""

    def update_session(self, session: ClaudeSession | None) -> None:
        if session is None:
            self.update("[dim]Select a session to see details[/]")
            return

        lines = []
        lines.append(f"[bold]{session.display_name}[/]  {session.activity_display}")
        lines.append("")
        lines.append(f"[dim]PID:[/]       {session.pid}")
        lines.append(f"[dim]Session:[/]   {session.session_id[:16]}...")
        lines.append(f"[dim]Slug:[/]      {session.slug}")
        lines.append(f"[dim]Directory:[/] {session.cwd}")
        lines.append(f"[dim]Started:[/]   {session.started_at_str}")
        lines.append(f"[dim]Runtime:[/]   {session.runtime_str}")
        lines.append(f"[dim]Messages:[/]  {session.message_count}")
        lines.append(f"[dim]Context:[/]   {session.context_display}")
        lines.append(f"[dim]Tokens:[/]    {session.input_tokens:,} in / {session.output_tokens:,} out")
        if session.git_branch:
            lines.append(f"[dim]Branch:[/]    {session.git_branch}")
        if session.last_message_time:
            lines.append(f"[dim]Last msg:[/]  {session.last_message_time}")
        if session.first_prompt:
            prompt_preview = session.first_prompt[:200]
            if len(session.first_prompt) > 200:
                prompt_preview += "..."
            lines.append("")
            lines.append(f"[bold]First prompt:[/]")
            lines.append(f"[italic]{prompt_preview}[/]")
        if session.last_prompt:
            prompt_preview = session.last_prompt[:200]
            if len(session.last_prompt) > 200:
                prompt_preview += "..."
            lines.append("")
            lines.append(f"[bold]Last prompt:[/]")
            lines.append(f"[italic]{prompt_preview}[/]")

        self.update("\n".join(lines))


class ClaudeMonitorApp(App):
    """Main TUI application."""

    TITLE = "Claude Code Monitor"
    SUB_TITLE = "Manage running Claude Code instances"

    CSS = """
    #main-container {
        height: 1fr;
    }

    #left-panel {
        width: 2fr;
        min-width: 55;
        border-right: solid $primary;
    }

    #right-panel {
        width: 3fr;
        min-width: 50;
    }

    #sessions-table {
        height: 1fr;
    }

    #detail-panel {
        height: auto;
        max-height: 50%;
        padding: 1;
        border-bottom: solid $primary;
    }

    #conversation-log {
        height: 1fr;
        padding: 0 1;
    }

    #message-input {
        dock: bottom;
        margin: 0 1;
        display: none;
    }

    #message-input.visible {
        display: block;
    }

    #totals-bar {
        height: auto;
        max-height: 5;
        padding: 0 1;
        background: $surface;
        border-top: solid $primary;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "focus_terminal", "Focus terminal"),
        Binding("s", "send_message", "Send message"),
        Binding("escape", "focus_table", "Back to list", show=True),
    ]

    sessions: reactive[list[ClaudeSession]] = reactive(list, recompose=False)
    selected_session: reactive[ClaudeSession | None] = reactive(None)
    _viewing_conversation: bool = False
    _conversation_session_id: str | None = None
    _conversation_timer = None
    _last_msg_count: int = 0
    _spinner_frame: int = 0
    _rebuilding_table: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            with Vertical(id="left-panel"):
                yield DataTable(id="sessions-table", cursor_type="row")
                yield Label("", id="totals-bar")
            with Vertical(id="right-panel"):
                yield SessionDetailPanel(id="detail-panel")
                yield RichLog(id="conversation-log", wrap=True, highlight=True, markup=True)
                yield Input(
                    placeholder="Type a message and press Enter (ESC to cancel)",
                    id="message-input",
                )
        yield Label("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.add_columns("Status", "Project", "PID", "Runtime", "Msgs", "Context")
        table.focus()
        self._do_refresh()
        self.set_interval(REFRESH_INTERVAL, self._do_refresh)
        self.set_interval(0.15, self._tick_spinner)
        self._start_conversation_refresh()

    def action_focus_table(self) -> None:
        """Return focus to the session list."""
        self._viewing_conversation = False
        self._conversation_session_id = None
        self._stop_conversation_refresh()
        self.query_one("#message-input", Input).remove_class("visible")
        table = self.query_one("#sessions-table", DataTable)
        table.focus()
        if self.selected_session:
            self.query_one("#detail-panel", SessionDetailPanel).update_session(
                self.selected_session
            )
        self._update_status_bar()

    def action_focus_terminal(self) -> None:
        """Bring the actual terminal window of the selected session to foreground."""
        if self.selected_session is None:
            self.notify("No session selected", severity="warning")
            return
        if not self.selected_session.is_alive:
            self.notify("Session is not running", severity="warning")
            return
        self._do_focus_terminal(self.selected_session)

    @work(thread=True)
    def _do_focus_terminal(self, session: ClaudeSession) -> None:
        ok, msg = focus_terminal_window(
            session.pid, slug=session.slug, cwd=session.cwd
        )
        if not ok:
            self.call_from_thread(
                self.notify, msg, severity="warning"
            )
        else:
            self.call_from_thread(
                self.notify, msg, severity="information"
            )

    def _tick_spinner(self) -> None:
        """Advance spinner animation for PROCESSING sessions."""
        self._spinner_frame += 1
        # only update status column cells, not full table refresh
        table = self.query_one("#sessions-table", DataTable)
        for i, s in enumerate(self.sessions):
            if s.activity_status == "processing":
                try:
                    table.update_cell_at(
                        (i, 0),
                        make_status_text(s.activity_status, self._spinner_frame),
                    )
                except Exception:
                    pass

    @work(thread=True)
    def _do_refresh(self) -> None:
        sessions = discover_sessions()
        self.call_from_thread(self._update_table, sessions)

    def _update_table(self, sessions: list[ClaudeSession]) -> None:
        self._rebuilding_table = True
        self.sessions = sessions
        table = self.query_one("#sessions-table", DataTable)

        selected_sid = None
        if self.selected_session:
            selected_sid = self.selected_session.session_id

        table.clear()
        for s in sessions:
            status = make_status_text(s.activity_status, self._spinner_frame)
            table.add_row(
                status,
                s.display_name,
                str(s.pid),
                s.runtime_str,
                str(s.message_count),
                s.context_display,
                key=s.session_id,
            )

        if selected_sid:
            for i, s in enumerate(sessions):
                if s.session_id == selected_sid:
                    table.move_cursor(row=i)
                    self.selected_session = s
                    break

        self._rebuilding_table = False
        self._update_totals_bar()
        self._update_status_bar()

    def _format_tokens(self, n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}k"
        return str(n)

    def _update_totals_bar(self) -> None:
        usage = get_usage_info()
        alive = [s for s in self.sessions if s.is_alive]
        avg_ctx = sum(s.context_pct for s in alive) / len(alive) if alive else 0
        max_ctx = max((s.context_pct for s in alive), default=0)

        parts = []
        parts.append(f"[bold]Weekly:[/] {usage.weekly_pct}%")
        parts.append(f"[bold]Session:[/] {usage.session_pct}%")
        parts.append(f"[bold]Ctx:[/] avg {avg_ctx:.0f}% max {max_ctx:.0f}%")

        self.query_one("#totals-bar", Label).update(" | ".join(parts))

    def _update_status_bar(self) -> None:
        alive = sum(1 for s in self.sessions if s.is_alive)
        processing = sum(1 for s in self.sessions if s.activity_status == "processing")
        waiting = sum(1 for s in self.sessions if s.activity_status == "waiting")
        total = len(self.sessions)

        if self._viewing_conversation:
            nav = "ESC: back | s: send | f: focus | q: quit"
        else:
            nav = "Enter: conversation | s: send | f: focus | r: refresh | q: quit"

        self.query_one("#status-bar", Label).update(
            f" \u25cf {alive} alive ({processing} processing, {waiting} waiting) / "
            f"{total} total | {nav}"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # skip events fired during table rebuild to prevent conversation flicker
        if self._rebuilding_table:
            return
        if event.row_key is None:
            return
        sid = str(event.row_key.value)
        for s in self.sessions:
            if s.session_id == sid:
                self.selected_session = s
                self.query_one("#detail-panel", SessionDetailPanel).update_session(s)
                self._conversation_session_id = s.session_id
                self._last_msg_count = 0
                self._load_conversation(s)
                break

    def action_refresh(self) -> None:
        self._do_refresh()

    def action_send_message(self) -> None:
        """Show message input for the selected session."""
        if self.selected_session is None:
            self.notify("No session selected", severity="warning")
            return
        if not self.selected_session.is_alive:
            self.notify("Session is not running", severity="warning")
            return
        msg_input = self.query_one("#message-input", Input)
        msg_input.add_class("visible")
        msg_input.value = ""
        msg_input.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle message submission."""
        if event.input.id != "message-input":
            return
        text = event.value.strip()
        event.input.value = ""
        event.input.remove_class("visible")

        if not text or self.selected_session is None:
            self.query_one("#sessions-table", DataTable).focus()
            return

        session = self.selected_session
        log = self.query_one("#conversation-log", RichLog)
        log.write(f"[bold blue]YOU[/] (sending...)")
        log.write(text)
        log.write("")
        log.focus()

        self._send_to_session(session, text)

    @work(thread=True)
    def _send_to_session(self, session: ClaudeSession, message: str) -> None:
        """Send a message to a Claude Code session via CLI."""
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-r", session.session_id,
                    "-p", message,
                    "--output-format", "text",
                    "--max-turns", "3",
                ],
                capture_output=True,
                text=True,
                cwd=session.cwd,
                timeout=300,
            )

            response = result.stdout.strip() if result.stdout else ""
            error = result.stderr.strip() if result.stderr else ""

            if response:
                self.call_from_thread(self._show_response, session, response)
            elif error:
                self.call_from_thread(self._show_response, session, f"[red]Error: {error}[/]")
            else:
                self.call_from_thread(self._show_response, session, "[dim](no response)[/]")

        except subprocess.TimeoutExpired:
            self.call_from_thread(
                self._show_response, session, "[yellow]Response timed out (5min)[/]"
            )
        except Exception as e:
            self.call_from_thread(
                self._show_response, session, f"[red]Error: {e}[/]"
            )

    def _show_response(self, session: ClaudeSession, response: str) -> None:
        log = self.query_one("#conversation-log", RichLog)
        log.write(f"[bold green]CLAUDE[/] (response)")
        log.write(response)
        log.write("")
        # refresh conversation to show updated JSONL
        self._last_msg_count = 0
        self._load_conversation(session)

    # -- conversation view with auto-refresh --

    def _start_conversation_refresh(self) -> None:
        self._stop_conversation_refresh()
        self._conversation_timer = self.set_interval(
            CONVERSATION_REFRESH_INTERVAL, self._refresh_conversation
        )

    def _stop_conversation_refresh(self) -> None:
        if self._conversation_timer is not None:
            self._conversation_timer.stop()
            self._conversation_timer = None

    def _refresh_conversation(self) -> None:
        """Auto-refresh: reload conversation for the currently viewed session."""
        if not self._viewing_conversation or not self._conversation_session_id:
            return
        for s in self.sessions:
            if s.session_id == self._conversation_session_id:
                self._load_conversation(s)
                break

    @work(thread=True)
    def _load_conversation(
        self, session: ClaudeSession, focus_log: bool = False
    ) -> None:
        messages = get_conversation_text(session, max_messages=40)
        self.call_from_thread(
            self._render_conversation, session, messages, focus_log
        )

    def _render_conversation(
        self,
        session: ClaudeSession,
        messages: list[ConversationMessage],
        focus_log: bool = False,
    ) -> None:
        # skip re-render if same session and same message count
        if (
            session.session_id == self._conversation_session_id
            and len(messages) == self._last_msg_count
            and not focus_log
        ):
            return

        self._last_msg_count = len(messages)
        self._conversation_session_id = session.session_id

        log = self.query_one("#conversation-log", RichLog)
        log.clear()
        log.write(
            f"[bold]Conversation: {session.display_name}[/] "
            f"({len(messages)} recent messages) "
            f"[dim]| auto-refreshing | f: focus terminal[/]\n"
        )

        for msg in messages:
            if msg.role == "user":
                log.write(f"[bold blue]USER[/] [dim]{msg.timestamp}[/]")
                text = msg.text
                if len(text) > 500:
                    text = text[:500] + "\n[dim]... (truncated)[/]"
                log.write(text)
                log.write("")
            elif msg.role == "assistant":
                if msg.msg_type == "tool_use":
                    log.write(f"[dim cyan]TOOLS[/] [dim]{msg.timestamp}[/]")
                    log.write(f"[dim]{msg.text}[/]")
                else:
                    log.write(f"[bold green]CLAUDE[/] [dim]{msg.timestamp}[/]")
                    text = msg.text
                    if len(text) > 1000:
                        text = text[:1000] + "\n[dim]... (truncated)[/]"
                    log.write(text)
                log.write("")

        if focus_log:
            self._viewing_conversation = True
            log.focus()
            self._start_conversation_refresh()

        self._update_status_bar()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter pressed: focus on conversation log for scrolling."""
        if event.row_key is None:
            return
        sid = str(event.row_key.value)
        for s in self.sessions:
            if s.session_id == sid:
                self.selected_session = s
                self.query_one("#detail-panel", SessionDetailPanel).update_session(s)
                self._load_conversation(s, focus_log=True)
                break


def main() -> None:
    app = ClaudeMonitorApp()
    app.run()


if __name__ == "__main__":
    main()

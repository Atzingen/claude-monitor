"""Claude Code Monitor - TUI dashboard for managing Claude Code instances."""

from __future__ import annotations

import subprocess
import sys

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from claude_monitor.sessions import ClaudeSession, discover_sessions, get_conversation_text


REFRESH_INTERVAL = 5.0


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
        if session.git_branch:
            lines.append(f"[dim]Branch:[/]    {session.git_branch}")
        if session.last_message_time:
            lines.append(f"[dim]Last msg:[/]  {session.last_message_time}")
        if session.first_prompt:
            prompt_preview = session.first_prompt[:300]
            if len(session.first_prompt) > 300:
                prompt_preview += "..."
            lines.append("")
            lines.append(f"[bold]First prompt:[/]")
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
        min-width: 50;
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
        Binding("c", "show_conversation", "Conversation"),
    ]

    sessions: reactive[list[ClaudeSession]] = reactive(list, recompose=False)
    selected_session: reactive[ClaudeSession | None] = reactive(None)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            with Vertical(id="left-panel"):
                yield DataTable(id="sessions-table", cursor_type="row")
            with Vertical(id="right-panel"):
                yield SessionDetailPanel(id="detail-panel")
                yield RichLog(id="conversation-log", wrap=True, highlight=True, markup=True)
        yield Label("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.add_columns(
            "Status", "Project", "PID", "Runtime", "Msgs"
        )
        self._do_refresh()
        self.set_interval(REFRESH_INTERVAL, self._do_refresh)

    @work(thread=True)
    def _do_refresh(self) -> None:
        sessions = discover_sessions()
        self.call_from_thread(self._update_table, sessions)

    def _update_table(self, sessions: list[ClaudeSession]) -> None:
        self.sessions = sessions
        table = self.query_one("#sessions-table", DataTable)

        selected_sid = None
        if self.selected_session:
            selected_sid = self.selected_session.session_id

        table.clear()
        for s in sessions:
            status_map = {
                "processing": "PROCESSING",
                "waiting": "WAITING",
                "stopped": "stopped",
                "unknown": "...",
            }
            status = status_map.get(s.activity_status, "?")
            table.add_row(
                status,
                s.display_name,
                str(s.pid),
                s.runtime_str,
                str(s.message_count),
                key=s.session_id,
            )

        if selected_sid:
            for i, s in enumerate(sessions):
                if s.session_id == selected_sid:
                    table.move_cursor(row=i)
                    break

        alive = sum(1 for s in sessions if s.is_alive)
        processing = sum(1 for s in sessions if s.activity_status == "processing")
        waiting = sum(1 for s in sessions if s.activity_status == "waiting")
        total = len(sessions)
        self.query_one("#status-bar", Label).update(
            f" {alive} alive ({processing} processing, {waiting} waiting) / "
            f"{total} total | r: refresh | c: conversation | q: quit"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        sid = str(event.row_key.value)
        for s in self.sessions:
            if s.session_id == sid:
                self.selected_session = s
                self.query_one("#detail-panel", SessionDetailPanel).update_session(s)
                break

    def action_refresh(self) -> None:
        self._do_refresh()

    def action_show_conversation(self) -> None:
        if self.selected_session is None:
            self.notify("No session selected", severity="warning")
            return
        self._load_conversation(self.selected_session)

    @work(thread=True)
    def _load_conversation(self, session: ClaudeSession) -> None:
        messages = get_conversation_text(session, max_messages=40)
        self.call_from_thread(self._render_conversation, session, messages)

    def _render_conversation(self, session: ClaudeSession, messages: list) -> None:
        log = self.query_one("#conversation-log", RichLog)
        log.clear()
        log.write(f"[bold]Conversation: {session.display_name}[/] ({len(messages)} recent messages)\n")

        for msg in messages:
            if msg.role == "user":
                if msg.msg_type == "tool_result":
                    continue
                log.write(f"[bold blue]USER[/] [dim]{msg.timestamp}[/]")
                # truncate very long user messages
                text = msg.text
                if len(text) > 500:
                    text = text[:500] + "\n[dim]... (truncated)[/]"
                log.write(text)
                log.write("")
            elif msg.role == "assistant":
                if msg.msg_type == "tool_use":
                    log.write(f"[bold magenta]CLAUDE[/] [dim]{msg.timestamp}[/]")
                else:
                    log.write(f"[bold green]CLAUDE[/] [dim]{msg.timestamp}[/]")
                text = msg.text
                if len(text) > 1000:
                    text = text[:1000] + "\n[dim]... (truncated)[/]"
                log.write(text)
                log.write("")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Load conversation on Enter/click."""
        if event.row_key is None:
            return
        sid = str(event.row_key.value)
        for s in self.sessions:
            if s.session_id == sid:
                self.selected_session = s
                self.query_one("#detail-panel", SessionDetailPanel).update_session(s)
                self._load_conversation(s)
                break


def main() -> None:
    app = ClaudeMonitorApp()
    app.run()


if __name__ == "__main__":
    main()

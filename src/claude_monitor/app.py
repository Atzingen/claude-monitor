"""Claude Code Monitor - TUI dashboard for managing Claude Code instances."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)

from claude_monitor.sessions import ClaudeSession, discover_sessions


REFRESH_INTERVAL = 3.0


class SessionDetailPanel(Static):
    """Shows details of the selected session."""

    def update_session(self, session: ClaudeSession | None) -> None:
        if session is None:
            self.update("[dim]Select a session to see details[/]")
            return

        lines = []
        lines.append(f"[bold]{session.display_name}[/]  {session.status_icon}")
        lines.append("")
        lines.append(f"[dim]PID:[/]       {session.pid}")
        lines.append(f"[dim]Session:[/]   {session.session_id[:12]}...")
        lines.append(f"[dim]Directory:[/] {session.cwd}")
        lines.append(f"[dim]Started:[/]   {session.started_at_str}")
        lines.append(f"[dim]Runtime:[/]   {session.runtime_str}")
        lines.append(f"[dim]Kind:[/]      {session.kind}")
        lines.append(f"[dim]Messages:[/]  {session.message_count}")
        if session.git_branch:
            lines.append(f"[dim]Branch:[/]    {session.git_branch}")
        if session.summary:
            lines.append("")
            lines.append(f"[bold]Summary:[/] {session.summary}")
        if session.first_prompt:
            prompt_preview = session.first_prompt[:200]
            if len(session.first_prompt) > 200:
                prompt_preview += "..."
            lines.append("")
            lines.append(f"[bold]First prompt:[/]")
            lines.append(f"[italic]{prompt_preview}[/]")

        self.update("\n".join(lines))


class InteractiveSession(ModalScreen[None]):
    """Full-screen interactive session with a Claude Code instance."""

    BINDINGS = [
        Binding("escape", "close", "Back to dashboard"),
    ]

    CSS = """
    InteractiveSession {
        align: center middle;
    }

    #interactive-container {
        width: 100%;
        height: 100%;
        background: $surface;
    }

    #session-header {
        dock: top;
        height: 1;
        background: $accent;
        color: $text;
        text-align: center;
        padding: 0 1;
    }

    #session-output {
        height: 1fr;
        border: solid $primary;
        margin: 0 1;
    }

    #session-input {
        dock: bottom;
        margin: 0 1 1 1;
    }
    """

    def __init__(self, session: ClaudeSession) -> None:
        super().__init__()
        self.session = session
        self._process: asyncio.subprocess.Process | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="interactive-container"):
            yield Label(
                f" Session: {self.session.display_name} | "
                f"PID: {self.session.pid} | "
                f"ESC to go back ",
                id="session-header",
            )
            yield RichLog(id="session-output", wrap=True, highlight=True, markup=True)
            yield Input(
                placeholder="Type a message to send to Claude Code (Enter to send)",
                id="session-input",
            )

    def on_mount(self) -> None:
        self._start_claude_session()
        self.query_one("#session-input", Input).focus()

    @work(thread=False)
    async def _start_claude_session(self) -> None:
        output = self.query_one("#session-output", RichLog)
        output.write("[bold]Connecting to Claude Code session...[/]")
        output.write(f"[dim]claude -r {self.session.session_id} --no-visual[/]")
        output.write("")

        try:
            self._process = await asyncio.create_subprocess_exec(
                "claude",
                "-r", self.session.session_id,
                "--no-visual",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await self._read_output()
        except FileNotFoundError:
            output.write("[red]Error: 'claude' not found in PATH[/]")
        except Exception as e:
            output.write(f"[red]Error: {e}[/]")

    async def _read_output(self) -> None:
        output = self.query_one("#session-output", RichLog)
        if self._process is None or self._process.stdout is None:
            return

        while True:
            try:
                line = await self._process.stdout.readline()
                if not line:
                    output.write("[dim]--- Session ended ---[/]")
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    output.write(text)
            except Exception:
                break

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return

        event.input.value = ""
        output = self.query_one("#session-output", RichLog)
        output.write(f"[bold green]> {value}[/]")

        if self._process is not None and self._process.stdin is not None:
            try:
                self._process.stdin.write(f"{value}\n".encode("utf-8"))
                await self._process.stdin.drain()
            except Exception as e:
                output.write(f"[red]Send error: {e}[/]")

    def action_close(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
        self.dismiss(None)


class NewSessionScreen(ModalScreen[str | None]):
    """Screen to start a new Claude Code session."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    NewSessionScreen {
        align: center middle;
    }

    #new-session-box {
        width: 80;
        height: auto;
        max-height: 20;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #new-session-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #dir-input {
        margin-bottom: 1;
    }

    #prompt-input {
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="new-session-box"):
            yield Label("New Claude Code Session", id="new-session-title")
            yield Label("Working directory:")
            yield Input(
                placeholder="e.g. C:\\Users\\Gustavo\\Desktop\\dev\\my-project",
                id="dir-input",
            )
            yield Label("Initial prompt (optional):")
            yield Input(
                placeholder="e.g. fix the login bug",
                id="prompt-input",
            )
            yield Label("[dim]Enter to start | ESC to cancel[/]")

    def on_mount(self) -> None:
        self.query_one("#dir-input", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "dir-input":
            self.query_one("#prompt-input", Input).focus()
            return

        dir_input = self.query_one("#dir-input", Input)
        prompt_input = self.query_one("#prompt-input", Input)

        directory = dir_input.value.strip()
        if not directory:
            return

        prompt = prompt_input.value.strip()
        cmd = f"cd {directory} && claude"
        if prompt:
            cmd = f'cd {directory} && claude -p "{prompt}"'

        self.dismiss(cmd)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ClaudeMonitorApp(App):
    """Main TUI application."""

    TITLE = "Claude Code Monitor"
    SUB_TITLE = "Manage running Claude Code instances"

    CSS = """
    #main-container {
        height: 1fr;
    }

    #left-panel {
        width: 1fr;
        min-width: 40;
        border-right: solid $primary;
    }

    #right-panel {
        width: 1fr;
        min-width: 40;
        padding: 1 2;
    }

    #sessions-table {
        height: 1fr;
    }

    #detail-panel {
        height: 1fr;
        padding: 1;
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
        Binding("enter", "connect", "Connect"),
        Binding("n", "new_session", "New Session"),
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
        yield Label("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.add_columns(
            "Status", "Project", "PID", "Runtime", "Messages", "Summary"
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

        # remember selected row
        selected_sid = None
        if self.selected_session:
            selected_sid = self.selected_session.session_id

        table.clear()
        for s in sessions:
            status = "RUNNING" if s.is_alive else "stopped"
            summary = (s.summary[:45] + "...") if len(s.summary) > 48 else s.summary
            table.add_row(
                status,
                s.display_name,
                str(s.pid),
                s.runtime_str,
                str(s.message_count),
                summary,
                key=s.session_id,
            )

        # restore selection
        if selected_sid:
            for i, s in enumerate(sessions):
                if s.session_id == selected_sid:
                    table.move_cursor(row=i)
                    break

        alive = sum(1 for s in sessions if s.is_alive)
        total = len(sessions)
        self.query_one("#status-bar", Label).update(
            f" {alive} running / {total} total sessions | "
            f"r: refresh | enter: connect | n: new | q: quit"
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

    def action_connect(self) -> None:
        if self.selected_session is None:
            self.notify("No session selected", severity="warning")
            return
        self.push_screen(InteractiveSession(self.selected_session))

    def action_new_session(self) -> None:
        def on_result(cmd: str | None) -> None:
            if cmd:
                self._launch_new_session(cmd)

        self.push_screen(NewSessionScreen(), callback=on_result)

    @work(thread=True)
    def _launch_new_session(self, cmd: str) -> None:
        try:
            subprocess.Popen(
                cmd,
                shell=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE
                if sys.platform == "win32"
                else 0,
            )
            self.call_from_thread(
                self.notify, "New session launched!", severity="information"
            )
        except Exception as e:
            self.call_from_thread(
                self.notify, f"Error: {e}", severity="error"
            )


def main() -> None:
    app = ClaudeMonitorApp()
    app.run()


if __name__ == "__main__":
    main()

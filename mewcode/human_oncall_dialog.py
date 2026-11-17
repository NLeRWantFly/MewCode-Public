from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static


class InlineHumanOnCallWidget(Vertical, can_focus=True):
    """Single-decision inline picker used by the human_oncall tool."""

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    class Responded(Message):
        def __init__(self, answers: dict[str, str] | None) -> None:
            super().__init__()
            self.answers = answers

    def __init__(self, question: dict, **kwargs) -> None:
        super().__init__(id="human-oncall-inline", **kwargs)
        self._question = question
        self._options = list(question.get("options", []))
        self._allow_other = bool(question.get("allowOther", True))
        self._cursor = 0
        self._other = ""

    def compose(self) -> ComposeResult:
        yield Static(self._render(), id="human-oncall-content")

    def on_mount(self) -> None:
        self.focus()

    def _count(self) -> int:
        return len(self._options) + (1 if self._allow_other else 0)

    def _render(self) -> str:
        question = escape(str(self._question.get("question", "需要你的决定")))
        context = escape(str(self._question.get("context", "")))
        lines = [f"[bold color(99)]{question}[/]"]
        if context:
            lines.extend(["", f"[dim]{context}[/]"])
        lines.append("")

        for index, option in enumerate(self._options):
            label = escape(
                str(option.get("label", option))
                if isinstance(option, dict)
                else str(option)
            )
            description = escape(
                str(option.get("description", ""))
                if isinstance(option, dict)
                else ""
            )
            recommended = (
                bool(option.get("recommended", False))
                if isinstance(option, dict)
                else False
            )
            pointer = "▸" if index == self._cursor else " "
            suffix = " [green](推荐)[/]" if recommended else ""
            detail = f" — [dim]{description}[/]" if description else ""
            display_label = (
                f"[bold]{label}[/]" if index == self._cursor else label
            )
            lines.append(f" {pointer} {display_label}{suffix}{detail}")

        if self._allow_other:
            other_index = len(self._options)
            pointer = "▸" if self._cursor == other_index else " "
            display_label = (
                "[bold]其他答案[/]"
                if self._cursor == other_index
                else "其他答案"
            )
            lines.append(f" {pointer} {display_label}")
            if self._cursor == other_index:
                value = (
                    escape(self._other)
                    if self._other
                    else "[dim]直接键入自定义答案…[/]"
                )
                lines.append(f"      {value}▌")

        lines.extend(["", "[dim]↑↓ 选择 · Enter 确认 · Esc 取消[/]"])
        return "\n".join(lines)

    def _refresh(self) -> None:
        self.query_one("#human-oncall-content", Static).update(self._render())

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()

    def action_cursor_down(self) -> None:
        if self._cursor < self._count() - 1:
            self._cursor += 1
            self._refresh()

    def action_select(self) -> None:
        name = str(self._question.get("name", "choice"))
        if self._cursor < len(self._options):
            option = self._options[self._cursor]
            if isinstance(option, dict):
                value = str(option.get("value", option.get("label", "")))
            else:
                value = str(option)
            self.post_message(self.Responded({name: value}))
            return
        if self._other.strip():
            self.post_message(self.Responded({name: self._other.strip()}))

    def action_cancel(self) -> None:
        self.post_message(self.Responded(None))

    def on_key(self, event) -> None:
        if not self._allow_other or self._cursor != len(self._options):
            return
        if event.key == "backspace":
            if self._other:
                self._other = self._other[:-1]
                self._refresh()
            event.stop()
        elif len(event.key) == 1 and event.key.isprintable():
            self._other += event.key
            self._refresh()
            event.stop()

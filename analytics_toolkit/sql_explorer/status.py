"""Compact query-state presentation for the SQL Explorer command pane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from textual.containers import Horizontal
from textual.widgets import Button, LoadingIndicator, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from .workspace import SqlExplorerWorkspace

_SLOW_QUERY_SECONDS = 300
_SECONDS_PER_MINUTE = 60
OutcomeStyle = Literal["running", "success", "error", "cancelled"]


@dataclass(frozen=True)
class QuerySummaryPresentation:
    outcome: str | None = None
    outcome_style: OutcomeStyle | None = None
    rows: str | None = None
    elapsed: str | None = None
    running: bool = False
    warning: str | None = None


def format_compact_duration(seconds: float) -> str:
    """Format an elapsed duration for a compact terminal status card."""
    seconds = max(0.0, seconds)
    if seconds < 1:
        return f"{seconds:.3f}s"
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds:.1f}".rstrip("0").rstrip(".") + "s"
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds_part:02d}s"
    return f"{minutes}m {seconds_part:02d}s"


def _row_summary(workspace: SqlExplorerWorkspace) -> str | None:
    result = workspace.last_run_result
    if result is None or result.dataframe is None:
        return None
    displayed = result.displayed_rows
    if result.truncated:
        if result.total_rows is None:
            return f"{displayed:,}+ rows"
        return f"{displayed:,} of {result.total_rows:,} rows"
    suffix = "row" if displayed == 1 else "rows"
    return f"{displayed:,} {suffix}"


def query_summary_for(workspace: SqlExplorerWorkspace) -> QuerySummaryPresentation:
    session = workspace.session
    active = getattr(session, "active_query", None)
    if workspace.query_state == "running":
        elapsed_seconds = active.elapsed_seconds if active is not None else 0.0
        return QuerySummaryPresentation(
            outcome="Query running",
            outcome_style="running",
            elapsed=format_compact_duration(elapsed_seconds),
            running=True,
            warning=(
                "consider optimizing your query or sit tight"
                if elapsed_seconds >= _SLOW_QUERY_SECONDS
                else None
            ),
        )

    if workspace.query_state in {"queued", "cancelling"}:
        return QuerySummaryPresentation()

    query = getattr(session, "last_query", None)
    if query is None or query.state not in {"completed", "failed", "cancelled"}:
        return QuerySummaryPresentation()
    outcomes: dict[str, tuple[str, OutcomeStyle]] = {
        "completed": ("✓ Query succeeded", "success"),
        "failed": ("✕ Query failed", "error"),
        "cancelled": ("⊘ Query cancelled", "cancelled"),
    }
    outcome, style = outcomes[query.state]
    return QuerySummaryPresentation(
        outcome=outcome,
        outcome_style=style,
        rows=_row_summary(workspace) if query.state == "completed" else None,
        elapsed=format_compact_duration(query.elapsed_seconds),
    )


class QuerySummaryBar(Horizontal):
    """Top command-pane strip containing query cards and interruption control."""

    _OUTCOME_CLASSES = ("running", "success", "error", "cancelled")

    def compose(self) -> ComposeResult:
        yield LoadingIndicator(id="query-running-indicator")
        yield Static("", id="query-outcome", classes="query-card", markup=False)
        yield Static("", id="query-rows", classes="query-card", markup=False)
        yield Static("", id="query-elapsed", classes="query-card", markup=False)
        yield Static("", id="query-warning", classes="query-card", markup=False)
        yield Button(
            "Interrupt",
            id="interrupt",
            classes="interrupt",
            disabled=True,
        )

    def update_presentation(self, presentation: QuerySummaryPresentation) -> None:
        indicator = self.query_one("#query-running-indicator", LoadingIndicator)
        indicator.styles.display = "block" if presentation.running else "none"
        self._update_card("#query-outcome", presentation.outcome)
        self._update_card("#query-rows", presentation.rows)
        self._update_card("#query-elapsed", presentation.elapsed)
        self._update_card("#query-warning", presentation.warning)
        outcome = self.query_one("#query-outcome", Static)
        for class_name in self._OUTCOME_CLASSES:
            outcome.remove_class(class_name)
        if presentation.outcome_style is not None:
            outcome.add_class(presentation.outcome_style)

    def _update_card(self, selector: str, value: str | None) -> None:
        card = self.query_one(selector, Static)
        card.update(value or "")
        card.styles.display = "block" if value else "none"


__all__ = [
    "QuerySummaryBar",
    "QuerySummaryPresentation",
    "format_compact_duration",
    "query_summary_for",
]

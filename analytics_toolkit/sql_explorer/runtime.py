from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic, sleep
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import pandas as pd

from analytics_toolkit import sql

from .errors import SqlExplorerConfigurationError
from .settings import (
    DEFAULT_RUN_BINDING,
    ExplorerSettings,
    load_settings,
    normalize_run_binding,
    save_settings,
)
from .statements import (
    DISPLAY_ROW_LIMIT,
    ExecutionRoute,
    ExplorerExecutionPlan,
    build_execution_plan,
)

if TYPE_CHECKING:
    from pathlib import Path

_CANCEL_LOOKUP_ATTEMPTS = 5
_CANCEL_LOOKUP_DELAY_SECONDS = 0.1
QueryState = Literal[
    "ready",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True)
class DatabaseSelection:
    connection_key: str
    backend: str


@dataclass(frozen=True)
class ExplorerRunResult:
    route: ExecutionRoute
    dataframe: pd.DataFrame | None
    displayed_rows: int
    total_rows: int | None
    truncated: bool
    status: str


@dataclass(frozen=True)
class ExplorerExportState:
    plan: ExplorerExecutionPlan
    dataframe: pd.DataFrame | None
    truncated: bool
    database: DatabaseSelection | None = None


@dataclass(frozen=True)
class ExplorerCancelResult:
    matched_queries: int
    cancelled_queries: int
    status: str


@dataclass(frozen=True)
class ExplorerQueryState:
    label: str
    route: ExecutionRoute
    started_at: float
    finished_at: float | None
    state: QueryState

    @property
    def elapsed_seconds(self) -> float:
        finished_at = monotonic() if self.finished_at is None else self.finished_at
        return max(0.0, finished_at - self.started_at)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs or not parts:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


class ExplorerSession:
    def __init__(self, db_key: str, *, settings_path: Path | None = None) -> None:
        loaded = load_settings(settings_path)
        self.settings = loaded.settings
        self.settings_warning = loaded.warning
        self.settings_path = settings_path
        self.database = validate_database(db_key)
        self.active_query_label: str | None = None
        self.active_query: ExplorerQueryState | None = None
        self.last_query: ExplorerQueryState | None = None
        self._cancellation_requested_for: str | None = None
        self._active_database: DatabaseSelection | None = None
        self._export_state: ExplorerExportState | None = None

    def fork(self) -> ExplorerSession:
        """Create an idle tab session with the current database and settings."""
        child = object.__new__(ExplorerSession)
        child.__dict__.update(
            settings=self.settings,
            settings_warning=None,
            settings_path=self.settings_path,
            database=self.database,
            active_query_label=None,
            active_query=None,
            last_query=None,
            _cancellation_requested_for=None,
            _active_database=None,
            _export_state=None,
        )
        return child

    def switch_database(self, db_key: str) -> DatabaseSelection:
        self.database = validate_database(db_key)
        self._export_state = None
        return self.database

    def set_run_binding(self, value: str) -> ExplorerSettings:
        binding = (
            DEFAULT_RUN_BINDING
            if value.strip().lower() == "reset"
            else normalize_run_binding(value)
        )
        self.settings = replace(self.settings, run_binding=binding)
        save_settings(self.settings, self.settings_path)
        return self.settings

    def set_confirmation(self, *, enabled: bool) -> ExplorerSettings:
        self.settings = replace(self.settings, confirm_mutations=bool(enabled))
        save_settings(self.settings, self.settings_path)
        return self.settings

    def set_query_concurrency(self, value: int) -> ExplorerSettings:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            message = "Query concurrency must be a positive integer."
            raise SqlExplorerConfigurationError(message)
        self.settings = replace(self.settings, max_concurrent_queries=value)
        save_settings(self.settings, self.settings_path)
        return self.settings

    def plan(self, sql_text: str) -> ExplorerExecutionPlan:
        return build_execution_plan(sql_text, self.database.backend)

    def execute(
        self,
        plan: ExplorerExecutionPlan,
        *,
        database: DatabaseSelection | None = None,
    ) -> ExplorerRunResult:
        self._export_state = None
        operation_database = database or self.database
        query_label = f"sql_explorer run={uuid4().hex}"
        self.active_query_label = query_label
        started_at = monotonic()
        self.active_query = ExplorerQueryState(query_label, plan.route, started_at, None, "running")
        self._active_database = operation_database
        self._cancellation_requested_for = None
        common_options = {
            "retry_cnt": 1,
            "timeout_increment": 0,
            "query_label": query_label,
            "return_metadata": True,
        }
        try:
            if plan.route is ExecutionRoute.READ:
                result = sql.read(
                    operation_database.connection_key,
                    plan.execution_sql,
                    **common_options,
                )
            elif plan.route is ExecutionRoute.EXECUTE_READ:
                result = sql.execute_read(
                    operation_database.connection_key, plan.execution_sql, **common_options
                )
            else:
                result = sql.execute(
                    operation_database.connection_key,
                    plan.execution_sql,
                    retry_policy="safe",
                    **common_options,
                )
            run_result, export_dataframe = self._build_run_result(plan, result)
        except BaseException:
            final_state: QueryState = (
                "cancelled" if self._cancellation_requested_for == query_label else "failed"
            )
            self.last_query = ExplorerQueryState(
                query_label,
                plan.route,
                started_at,
                monotonic(),
                final_state,
            )
            raise
        else:
            self.last_query = ExplorerQueryState(
                query_label, plan.route, started_at, monotonic(), "completed"
            )
        finally:
            if self.active_query_label == query_label:
                self.active_query_label = None
            if self.active_query and self.active_query.label == query_label:
                self.active_query = None
            if self._cancellation_requested_for == query_label:
                self._cancellation_requested_for = None
            if self.active_query_label is None:
                self._active_database = None
        if run_result.dataframe is not None:
            self._export_state = ExplorerExportState(
                plan,
                export_dataframe,
                run_result.truncated,
                operation_database,
            )
        return run_result

    def export_state(self) -> ExplorerExportState:
        if self._export_state is None:
            raise _missing_export_result_error()
        return self._export_state

    def export_dataframe(self) -> pd.DataFrame:
        state = self.export_state()
        if state.dataframe is not None:
            return state.dataframe.copy()

        plan = state.plan
        operation_database = state.database or self.database
        query_label = f"sql_explorer export={uuid4().hex}"
        started_at = monotonic()
        self.active_query_label = query_label
        self.active_query = ExplorerQueryState(query_label, plan.route, started_at, None, "running")
        self._active_database = operation_database
        self._cancellation_requested_for = None
        common_options = {
            "retry_cnt": 1,
            "timeout_increment": 0,
            "query_label": query_label,
            "return_metadata": True,
        }
        try:
            if plan.route is ExecutionRoute.READ:
                result = sql.read(
                    operation_database.connection_key,
                    plan.full_execution_sql,
                    **common_options,
                )
            else:
                result = sql.execute_read(
                    operation_database.connection_key,
                    plan.full_execution_sql,
                    **common_options,
                )
            dataframe = self._result_dataframe(result)
        except BaseException:
            final_state: QueryState = (
                "cancelled" if self._cancellation_requested_for == query_label else "failed"
            )
            self.last_query = ExplorerQueryState(
                query_label, plan.route, started_at, monotonic(), final_state
            )
            raise
        else:
            self.last_query = ExplorerQueryState(
                query_label, plan.route, started_at, monotonic(), "completed"
            )
        finally:
            if self.active_query_label == query_label:
                self.active_query_label = None
            if self.active_query and self.active_query.label == query_label:
                self.active_query = None
            if self._cancellation_requested_for == query_label:
                self._cancellation_requested_for = None
            if self.active_query_label is None:
                self._active_database = None
        return dataframe

    @staticmethod
    def _result_dataframe(result: object) -> pd.DataFrame:
        dataframe = getattr(result, "data", result)
        if not isinstance(dataframe, pd.DataFrame):
            message = "Result-producing SQL did not return a dataframe."
            raise TypeError(message)
        return dataframe

    @staticmethod
    def _build_run_result(
        plan: ExplorerExecutionPlan,
        result: object,
    ) -> tuple[ExplorerRunResult, pd.DataFrame | None]:
        if plan.returns_rows:
            dataframe = ExplorerSession._result_dataframe(result)
            raw_rows = len(dataframe)
            truncated = raw_rows > DISPLAY_ROW_LIMIT
            displayed = dataframe.head(DISPLAY_ROW_LIMIT).copy()
            total_rows = None if truncated and plan.server_limited else raw_rows
            if truncated and total_rows is None:
                status = f"Showing the first {DISPLAY_ROW_LIMIT} rows; more rows are available."
            elif truncated:
                status = f"Showing the first {DISPLAY_ROW_LIMIT} of {total_rows:,} rows."
            else:
                status = f"Returned {raw_rows:,} row(s)."
            return (
                ExplorerRunResult(
                    route=plan.route,
                    dataframe=displayed,
                    displayed_rows=len(displayed),
                    total_rows=total_rows,
                    truncated=truncated,
                    status=status,
                ),
                None if truncated and plan.server_limited else dataframe.copy(),
            )

        return (
            ExplorerRunResult(
                route=plan.route,
                dataframe=None,
                displayed_rows=0,
                total_rows=None,
                truncated=False,
                status=f"Executed {plan.statement_count} statement(s) successfully.",
            ),
            None,
        )

    def cancel_active(self) -> ExplorerCancelResult:
        query_label = self.active_query_label
        if query_label is None:
            return ExplorerCancelResult(0, 0, "No active explorer query was found.")
        if self.active_query is not None and self.active_query.label == query_label:
            self.active_query = replace(self.active_query, state="cancelling")

        database = self._active_database or self.database
        query_ids: list[int | str] = []
        for attempt in range(_CANCEL_LOOKUP_ATTEMPTS):  # pragma: no branch
            running = sql.show_queries(
                database.connection_key,
                state="active",
                retry_cnt=1,
                timeout_increment=0,
                query_label=f"sql_explorer cancel_lookup={uuid4().hex}",
            )
            if "query" in running and "query_id" in running:
                matches = (
                    running["query"]
                    .fillna("")
                    .astype(str)
                    .str.contains(
                        query_label,
                        regex=False,
                    )
                )
                query_ids = running.loc[matches, "query_id"].tolist()
            if query_ids or attempt == _CANCEL_LOOKUP_ATTEMPTS - 1:
                break
            sleep(_CANCEL_LOOKUP_DELAY_SECONDS)

        if not query_ids:
            if self.active_query is not None and self.active_query.label == query_label:
                self.active_query = replace(self.active_query, state="running")
            return ExplorerCancelResult(
                0,
                0,
                "The active explorer query finished or was not visible; nothing was cancelled.",
            )

        self._cancellation_requested_for = query_label
        try:
            cancelled = sql.cancel_queries(
                database.connection_key,
                query_ids,
                retry_cnt=1,
                timeout_increment=0,
                query_label=f"sql_explorer cancel={uuid4().hex}",
            )
        except BaseException:
            self._cancellation_requested_for = None
            if self.active_query is not None and self.active_query.label == query_label:
                self.active_query = replace(self.active_query, state="running")
            raise
        cancelled_count = sum(
            bool(getattr(row, "cancelled", False) or getattr(row, "terminated", False))
            for row in cancelled.itertuples(index=False)
        )
        return ExplorerCancelResult(
            matched_queries=len(query_ids),
            cancelled_queries=cancelled_count,
            status=(
                f"Cancellation requested for {cancelled_count} of "
                f"{len(query_ids)} matching query(s)."
            ),
        )


def _missing_export_result_error() -> SqlExplorerConfigurationError:
    return SqlExplorerConfigurationError("Run a row-producing query before exporting.")


def validate_database(db_key: str) -> DatabaseSelection:
    results = sql.validate_connections([db_key], connect=False)
    if not results:
        message = f"SQL connection {db_key!r} was not found."
        raise SqlExplorerConfigurationError(message)
    result = results[0]
    if not result.valid or result.backend is None:
        details = f": {result.error}" if result.error else ""
        message = f"SQL connection {result.connection_key!r} is invalid{details}"
        raise SqlExplorerConfigurationError(message)
    return DatabaseSelection(result.connection_key, result.backend)


__all__ = [
    "DatabaseSelection",
    "ExplorerCancelResult",
    "ExplorerExportState",
    "ExplorerQueryState",
    "ExplorerRunResult",
    "ExplorerSession",
    "QueryState",
    "format_duration",
    "validate_database",
]

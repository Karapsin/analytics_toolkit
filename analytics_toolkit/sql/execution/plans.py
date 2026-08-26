from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .labels import apply_query_label


@dataclass(frozen=True)
class SqlStatement:
    sql: str
    alias: str | None = None
    backend: str | None = None
    phase: str | None = None
    target_table: str | None = None
    source_table: str | None = None


@dataclass
class SqlOperationMetadata:
    transfer_id: str | None = None
    source_rows: int | None = None
    expected_source_rows: int | None = None
    streamed_rows: int | None = None
    staged_rows: int | None = None
    stage_rows: int | None = None
    row_count_validated: bool | None = None
    transfer_slice_counts: list[dict[str, Any]] | None = None
    inserted_rows: int | None = None
    affected_rows: int | None = None
    final_target_rows: int | None = None
    stage_table: str | None = None
    elapsed_seconds: float | None = None
    retry_attempts: int | None = None
    read_rows: int | None = None
    statement_count: int | None = None
    operation_status: str | None = None
    query_label: str | None = None
    stage_external_location: str | None = None
    worker_stage_count: int | None = None
    stage_tables: list[str] | None = None
    aggregate_stage_table: str | None = None
    requested_read_concurrency: int | None = None
    requested_write_concurrency: int | None = None
    effective_read_concurrency: int | None = None
    effective_write_concurrency: int | None = None
    ignore_source_staging: bool | None = None
    source_staging_mode: str | None = None
    source_stage_count: int | None = None
    soft_limited_read_concurrency: int | None = None
    soft_limited_write_concurrency: int | None = None
    soft_concurrency_cap: int | None = None
    hard_concurrency_cap: int | None = None
    live_source_stage_limit: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "source_rows": self.source_rows,
            "expected_source_rows": self.expected_source_rows,
            "streamed_rows": self.streamed_rows,
            "staged_rows": self.staged_rows,
            "stage_rows": self.stage_rows,
            "row_count_validated": self.row_count_validated,
            "transfer_slice_counts": self.transfer_slice_counts,
            "inserted_rows": self.inserted_rows,
            "affected_rows": self.affected_rows,
            "final_target_rows": self.final_target_rows,
            "stage_table": self.stage_table,
            "elapsed_seconds": self.elapsed_seconds,
            "retry_attempts": self.retry_attempts,
            "read_rows": self.read_rows,
            "statement_count": self.statement_count,
            "operation_status": self.operation_status,
            "query_label": self.query_label,
            "stage_external_location": self.stage_external_location,
            "worker_stage_count": self.worker_stage_count,
            "stage_tables": self.stage_tables,
            "aggregate_stage_table": self.aggregate_stage_table,
            "requested_read_concurrency": self.requested_read_concurrency,
            "requested_write_concurrency": self.requested_write_concurrency,
            "soft_limited_read_concurrency": self.soft_limited_read_concurrency,
            "soft_limited_write_concurrency": self.soft_limited_write_concurrency,
            "soft_concurrency_cap": self.soft_concurrency_cap,
            "hard_concurrency_cap": self.hard_concurrency_cap,
            "effective_read_concurrency": self.effective_read_concurrency,
            "effective_write_concurrency": self.effective_write_concurrency,
            "ignore_source_staging": self.ignore_source_staging,
            "source_staging_mode": self.source_staging_mode,
            "source_stage_count": self.source_stage_count,
            "live_source_stage_limit": self.live_source_stage_limit,
        }


@dataclass
class SqlPlan:
    operation: str
    statements: list[SqlStatement] = field(default_factory=list)
    source_alias: str | None = None
    target_alias: str | None = None
    source_backend: str | None = None
    target_backend: str | None = None
    source_table: str | None = None
    target_table: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    metadata: SqlOperationMetadata = field(default_factory=SqlOperationMetadata)

    @property
    def sqls(self) -> list[str]:
        return [statement.sql for statement in self.statements]

    def add(
        self,
        sql: str,
        *,
        alias: str | None = None,
        backend: str | None = None,
        phase: str | None = None,
        target_table: str | None = None,
        source_table: str | None = None,
        query_label: str | None = None,
    ) -> None:
        prepared_sql = apply_query_label(sql, query_label)
        if alias is not None and backend is not None:
            from analytics_toolkit.sql.backends import get_backend_adapter  # noqa: PLC0415

            prepare_plan_sql = getattr(
                get_backend_adapter(backend),
                "prepare_plan_sql",
                None,
            )
            if prepare_plan_sql is not None:
                prepared_sql = prepare_plan_sql(
                    alias,
                    prepared_sql,
                    [statement.sql for statement in self.statements],
                )
        self.statements.append(
            SqlStatement(
                sql=prepared_sql,
                alias=alias,
                backend=backend,
                phase=phase,
                target_table=target_table,
                source_table=source_table,
            )
        )

    def extend(
        self,
        statements: list[str],
        *,
        alias: str | None = None,
        backend: str | None = None,
        phase: str | None = None,
        target_table: str | None = None,
        source_table: str | None = None,
        query_label: str | None = None,
    ) -> None:
        for statement in statements:
            self.add(
                statement,
                alias=alias,
                backend=backend,
                phase=phase,
                target_table=target_table,
                source_table=source_table,
                query_label=query_label,
            )


@dataclass
class SqlOperationResult:
    rows: Any
    metadata: SqlOperationMetadata
    plan: SqlPlan | None = None
    data: Any | None = None

    @property
    def inserted_rows(self) -> int | None:
        return self.metadata.inserted_rows

    @property
    def affected_rows(self) -> int | None:
        return self.metadata.affected_rows


def format_plan(
    plan: SqlPlan,
    *,
    include_sql: bool = True,
    max_sql_chars: int = 160,
) -> str:
    from .validation import validate_positive_int

    if not isinstance(plan, SqlPlan):
        raise TypeError("plan must be a SqlPlan.")
    validate_positive_int(max_sql_chars, "max_sql_chars")

    lines = [
        f"SqlPlan: {plan.operation}",
        (
            "Source: "
            f"alias={_format_empty(plan.source_alias)} "
            f"backend={_format_empty(plan.source_backend)} "
            f"table={_format_empty(plan.source_table)}"
        ),
        (
            "Target: "
            f"alias={_format_empty(plan.target_alias)} "
            f"backend={_format_empty(plan.target_backend)} "
            f"table={_format_empty(plan.target_table)}"
        ),
        f"Metadata: {_format_metadata(plan)}",
        f"Options: {_format_options(plan.options)}",
        "Statements:",
    ]

    if not plan.statements:
        lines.append("  <none>")
        return "\n".join(lines)

    for index, statement in enumerate(plan.statements, start=1):
        row = (
            f"  {index}. "
            f"phase={_format_empty(statement.phase)} "
            f"alias={_format_empty(statement.alias)} "
            f"backend={_format_empty(statement.backend)} "
            f"source={_format_empty(statement.source_table)} "
            f"target={_format_empty(statement.target_table)}"
        )
        if include_sql:
            row = f"{row} sql={_short_sql_preview(statement.sql, max_sql_chars)}"
        lines.append(row)
    return "\n".join(lines)


def _format_metadata(plan: SqlPlan) -> str:
    metadata = plan.metadata.as_dict()
    if metadata["statement_count"] is None:
        metadata["statement_count"] = len(plan.statements)
    populated = {
        key: value
        for key, value in metadata.items()
        if value is not None or key == "statement_count"
    }
    return _format_mapping(populated)


def _format_options(options: dict[str, Any]) -> str:
    if not options:
        return "<none>"
    return _format_mapping(options)


def _format_mapping(values: dict[str, Any]) -> str:
    if not values:
        return "<none>"
    return ", ".join(f"{key}={_format_value(values[key])}" for key in sorted(values))


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    return repr(value)


def _format_empty(value: str | None) -> str:
    return "-" if value is None else str(value)


def _short_sql_preview(sql: str, max_sql_chars: int) -> str:
    preview = " ".join(str(sql).split())
    if len(preview) <= max_sql_chars:
        return preview
    if max_sql_chars <= 3:
        return preview[:max_sql_chars]
    return preview[: max_sql_chars - 3].rstrip() + "..."

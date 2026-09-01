from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.execution.operation_runner import tracked_sql_operation

if TYPE_CHECKING:
    from analytics_toolkit.sql.ddl.models import CreateSqlTableOptions
    from analytics_toolkit.sql.execution.plans import SqlOperationMetadata


def build_drop_target_sqls(options: CreateSqlTableOptions) -> list[str]:
    if not options.drop_target_if_exists:
        return []
    return cast(
        "list[str]",
        get_backend_adapter(options.backend).build_drop_target_sqls(
            options.table_name,
            ch_cluster=options.ch_cluster,
            ch_only_shard=options.ch_only_shard,
            query_label=options.query_label,
        ),
    )


def drop_existing_target(
    *,
    options: CreateSqlTableOptions,
    connection: Any,
    drop_sqls: list[str],
    metadata: SqlOperationMetadata,
    retry_attempt: int | None,
) -> None:
    if not options.drop_target_if_exists:
        return
    with tracked_sql_operation(
        metadata=metadata,
        operation_name="create_table",
        alias=options.connection_key,
        backend=options.backend,
        phase="drop_target",
        retry_attempt=retry_attempt,
        query_label=options.query_label,
        preview_sql=drop_sqls[0] if drop_sqls else None,
    ):
        get_backend_adapter(options.backend).prepare_existing_target_for_create_from_sql(
            connection,
            options.table_name,
            drop_target_if_exists=True,
            ch_cluster=options.ch_cluster,
            ch_only_shard=options.ch_only_shard,
            query_label=options.query_label,
            connection_key=options.connection_key,
        )

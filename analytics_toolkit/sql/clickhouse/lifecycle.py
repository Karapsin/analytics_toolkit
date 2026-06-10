from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from ..backend_adapters import ch_cluster_clause, get_backend_adapter
from .options import DEFAULT_CH_PER_HOST_DROP_WORKERS
from .wait import (
    _normalize_non_empty_string,
    _query_ch_cluster_table_rows,
    _resolve_ch_cluster_name_for_wait,
    _sql_string_literal,
    _wait_for_ch_distributed_table_pair,
    _wait_for_ch_distributed_table_pair_absence,
    _wait_for_ch_table_absence,
    _wait_for_ch_table_absence_on_cluster,
)
from ..ddl.clickhouse import (
    build_ch_distributed_create_table_sqls,
    build_ch_shard_table_name,
)
from ..execution.labels import apply_query_label


@dataclass(frozen=True)
class ChDistributedTablePair:
    distributed_table: str
    shard_table: str


def ch_distributed_table_pair(
    table_name: str,
    shard_table: str | None = None,
) -> ChDistributedTablePair:
    return ChDistributedTablePair(
        distributed_table=table_name,
        shard_table=shard_table or build_ch_shard_table_name(table_name),
    )


def build_drop_ch_distributed_table_pair_sqls(
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
    if_exists: bool = True,
) -> list[str]:
    pair = ch_distributed_table_pair(table_name, shard_table)
    sqls = [
        _build_drop_ch_table_sql(
            pair.distributed_table,
            query_label=query_label,
            if_exists=if_exists,
        ),
        _build_drop_ch_table_sql(
            pair.shard_table,
            query_label=query_label,
            if_exists=if_exists,
        ),
    ]
    if ch_cluster is not None:
        sqls.extend(
            [
                _build_drop_ch_table_sql(
                    pair.distributed_table,
                    ch_cluster=ch_cluster,
                    query_label=query_label,
                    if_exists=if_exists,
                ),
                _build_drop_ch_table_sql(
                    pair.shard_table,
                    ch_cluster=ch_cluster,
                    query_label=query_label,
                    if_exists=if_exists,
                ),
            ]
        )
    return sqls


def build_drop_ch_table_sqls(
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    query_label: str | None = None,
    if_exists: bool = True,
) -> list[str]:
    sqls = [
        _build_drop_ch_table_sql(
            table_name,
            query_label=query_label,
            if_exists=if_exists,
        )
    ]
    if ch_cluster is not None:
        sqls.append(
            _build_drop_ch_table_sql(
                table_name,
                ch_cluster=ch_cluster,
                query_label=query_label,
                if_exists=if_exists,
            )
        )
    return sqls


def drop_ch_distributed_table_pair(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
    if_exists: bool = True,
    wait_for_absence: bool = False,
    wait_timeout_seconds: int = 300,
    wait_poll_interval_seconds: float = 1,
    ch_retry_per_host_drops: bool = True,
    per_host_connection_factory: Callable[[str], Any] | None = None,
) -> None:
    pair = ch_distributed_table_pair(table_name, shard_table)
    _execute_ch_sqls(
        connection,
        build_drop_ch_distributed_table_pair_sqls(
            pair.distributed_table,
            ch_cluster=ch_cluster,
            shard_table=pair.shard_table,
            query_label=query_label,
            if_exists=if_exists,
        ),
    )
    if wait_for_absence:
        try:
            _wait_for_ch_distributed_table_pair_absence(
                connection,
                pair.distributed_table,
                ch_cluster=ch_cluster,
                timeout_seconds=wait_timeout_seconds,
                poll_interval_seconds=wait_poll_interval_seconds,
            )
            return
        except TimeoutError as exc:
            if not ch_retry_per_host_drops:
                raise
            if ch_cluster is None:
                raise TimeoutError(
                    f"{exc} ch_retry_per_host_drops=True requires a non-null "
                    "ch_cluster."
                ) from exc
            if per_host_connection_factory is None:
                raise TimeoutError(
                    f"{exc} ch_retry_per_host_drops=True requires a per-host "
                    "ClickHouse connection factory."
                ) from exc

        _drop_ch_distributed_table_pair_on_cluster_hosts(
            connection,
            pair,
            ch_cluster=ch_cluster,
            query_label=query_label,
            per_host_drop_workers=DEFAULT_CH_PER_HOST_DROP_WORKERS,
            per_host_connection_factory=per_host_connection_factory,
        )
        _wait_for_ch_distributed_table_pair_absence(
            connection,
            pair.distributed_table,
            ch_cluster=ch_cluster,
            timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=wait_poll_interval_seconds,
        )


def drop_ch_table(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    query_label: str | None = None,
    if_exists: bool = True,
    wait_for_absence: bool = False,
    wait_timeout_seconds: int = 300,
    wait_poll_interval_seconds: float = 1,
) -> None:
    _execute_ch_sqls(
        connection,
        build_drop_ch_table_sqls(
            table_name,
            ch_cluster=ch_cluster,
            query_label=query_label,
            if_exists=if_exists,
        ),
    )
    if not wait_for_absence:
        return

    if ch_cluster is None:
        _wait_for_ch_table_absence(
            connection,
            table_name,
            timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=wait_poll_interval_seconds,
        )
        return

    _wait_for_ch_table_absence_on_cluster(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        timeout_seconds=wait_timeout_seconds,
        poll_interval_seconds=wait_poll_interval_seconds,
    )


def build_truncate_ch_distributed_table_pair_sqls(
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
) -> list[str]:
    pair = ch_distributed_table_pair(table_name, shard_table)
    return [
        _build_truncate_ch_table_sql(
            pair.shard_table,
            ch_cluster=ch_cluster,
            query_label=query_label,
        ),
        _build_truncate_ch_table_sql(
            pair.distributed_table,
            query_label=query_label,
        ),
    ]


def truncate_ch_distributed_table_pair(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
) -> None:
    _execute_ch_sqls(
        connection,
        build_truncate_ch_distributed_table_pair_sqls(
            table_name,
            ch_cluster=ch_cluster,
            shard_table=shard_table,
            query_label=query_label,
        ),
    )


def build_create_ch_distributed_table_pair_sqls(
    *,
    table_name: str,
    joined_columns: str,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = False,
    query_label: str | None = None,
) -> list[str]:
    return [
        apply_query_label(sql, query_label)
        for sql in build_ch_distributed_create_table_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_replace_table=ch_replace_table,
        )
    ]


def create_ch_distributed_table_pair(
    connection: Any,
    *,
    table_name: str,
    joined_columns: str,
    partition_by: Sequence[str] | str | None = None,
    order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = False,
    query_label: str | None = None,
    wait_for_table: bool = False,
) -> None:
    _execute_ch_sqls(
        connection,
        build_create_ch_distributed_table_pair_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            partition_by=partition_by,
            order_by=order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_replace_table=ch_replace_table,
            query_label=query_label,
        ),
    )
    if wait_for_table:
        _wait_for_ch_distributed_table_pair(
            connection,
            table_name,
            ch_cluster=ch_cluster,
        )


def _build_drop_ch_table_sql(
    table_name: str,
    *,
    ch_cluster: str | None = None,
    query_label: str | None = None,
    if_exists: bool = True,
) -> str:
    prefix = "DROP TABLE IF EXISTS" if if_exists else "DROP TABLE"
    return apply_query_label(
        f"{prefix} {table_name}{ch_cluster_clause(ch_cluster)}",
        query_label,
    )


def _build_truncate_ch_table_sql(
    table_name: str,
    *,
    ch_cluster: str | None = None,
    query_label: str | None = None,
) -> str:
    return apply_query_label(
        f"TRUNCATE TABLE IF EXISTS {table_name}{ch_cluster_clause(ch_cluster)}",
        query_label,
    )


def _execute_ch_sqls(connection: Any, sqls: list[str]) -> None:
    adapter = get_backend_adapter("ch")
    for sql in sqls:
        adapter.execute_command(connection, sql)


def _drop_ch_distributed_table_pair_on_cluster_hosts(
    connection: Any,
    pair: ChDistributedTablePair,
    *,
    ch_cluster: str,
    query_label: str | None,
    per_host_drop_workers: int,
    per_host_connection_factory: Callable[[str], Any],
) -> None:
    configured_hosts = _query_ch_configured_cluster_hosts(connection, ch_cluster)
    hosts = _select_ch_hosts_for_local_drop(
        connection,
        pair,
        ch_cluster=ch_cluster,
        configured_hosts=configured_hosts,
    )
    if not hosts:
        raise TimeoutError(
            "ch_retry_per_host_drops=True could not find any configured "
            f"ClickHouse hosts for cluster {ch_cluster!r}."
        )

    max_workers = min(per_host_drop_workers, len(hosts))
    error_by_host: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_host = {
            executor.submit(
                _drop_ch_distributed_table_pair_on_host,
                host,
                pair=pair,
                query_label=query_label,
                per_host_connection_factory=per_host_connection_factory,
            ): host
            for host in hosts
        }
        for future in as_completed(future_to_host):
            host = future_to_host[future]
            try:
                error = future.result()
            except Exception as exc:
                error = f"{host}: {exc!r}"
            if error is not None:
                error_by_host[host] = error

    errors = [error_by_host[host] for host in hosts if host in error_by_host]
    if errors:
        raise TimeoutError(
            "ch_retry_per_host_drops=True failed to locally drop ClickHouse "
            f"table pair {pair.distributed_table} / {pair.shard_table} on "
            "some host(s): " + "; ".join(errors)
        )


def _drop_ch_distributed_table_pair_on_host(
    host: str,
    *,
    pair: ChDistributedTablePair,
    query_label: str | None,
    per_host_connection_factory: Callable[[str], Any],
) -> str | None:
    host_connection = None
    try:
        host_connection = per_host_connection_factory(host)
        _execute_ch_sqls(
            host_connection,
            [
                _build_drop_ch_table_sql(
                    pair.distributed_table,
                    query_label=query_label,
                ),
                _build_drop_ch_table_sql(
                    pair.shard_table,
                    query_label=query_label,
                ),
            ],
        )
    except Exception as exc:
        return f"{host}: {exc!r}"
    finally:
        if host_connection is not None:
            close = getattr(host_connection, "close", None)
            if callable(close):
                close()
    return None


def _query_ch_configured_cluster_hosts(
    connection: Any,
    ch_cluster: str,
) -> list[str]:
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    sql = (
        "SELECT DISTINCT host_name\n"
        "FROM system.clusters\n"
        f"WHERE cluster = {_sql_string_literal(cluster_name)}\n"
        "ORDER BY host_name"
    )
    result = connection.query(sql)
    rows = getattr(result, "result_rows", None) or []
    hosts: list[str] = []
    for row in rows:
        if not row:
            continue
        host = str(row[0]).strip()
        if host:
            hosts.append(host)
    return hosts


def _select_ch_hosts_for_local_drop(
    connection: Any,
    pair: ChDistributedTablePair,
    *,
    ch_cluster: str,
    configured_hosts: Sequence[str],
) -> list[str]:
    if not configured_hosts:
        return []

    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    try:
        leftover_rows = _query_ch_cluster_table_rows(
            connection,
            table_names=[pair.distributed_table, pair.shard_table],
            ch_cluster=cluster_name,
        )
    except Exception:
        return list(configured_hosts)

    leftover_hosts = {
        str(row[0]).strip()
        for row in leftover_rows
        if row and str(row[0]).strip()
    }
    if not leftover_hosts:
        return list(configured_hosts)

    configured_host_set = set(configured_hosts)
    if not leftover_hosts <= configured_host_set:
        return list(configured_hosts)

    return [host for host in configured_hosts if host in leftover_hosts]

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .ddl.create_sql_table import (
    _normalize_non_empty_string,
    _sql_string_literal,
    build_ch_shard_table_name,
    normalize_table_schema,
    split_ch_table_name_for_distributed_engine,
)


def _wait_for_ch_table(
    connection: Any,
    table_name: str,
    timeout_seconds: int = 60,
    poll_interval_seconds: float = 1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = connection.query(f"EXISTS TABLE {table_name}")
        if result.result_rows and result.result_rows[0][0]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ClickHouse table {table_name} was not visible after "
                f"{timeout_seconds} second(s)."
            )
        time.sleep(poll_interval_seconds)


def _wait_for_ch_distributed_table_pair(
    connection: Any,
    table_name: str,
    ch_cluster: str = "{cluster}",
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
    expected_column_types: Mapping[str, str] | None = None,
) -> None:
    shard_table = build_ch_shard_table_name(table_name)
    _wait_for_ch_table(
        connection,
        table_name,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    _wait_for_ch_table(
        connection,
        shard_table,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    _wait_for_ch_table_on_cluster(
        connection,
        table_name,
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    _wait_for_ch_table_on_cluster(
        connection,
        shard_table,
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if expected_column_types is not None:
        _wait_for_ch_table_schema_on_cluster(
            connection,
            table_name,
            expected_column_types=expected_column_types,
            ch_cluster=ch_cluster,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        _wait_for_ch_table_schema_on_cluster(
            connection,
            shard_table,
            expected_column_types=expected_column_types,
            ch_cluster=ch_cluster,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )


def _wait_for_ch_distributed_table_pair_absence(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    shard_table = build_ch_shard_table_name(table_name)
    if ch_cluster is None:
        _wait_for_ch_table_absence(
            connection,
            table_name,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        _wait_for_ch_table_absence(
            connection,
            shard_table,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        return

    _wait_for_ch_tables_absence_on_cluster(
        connection,
        [table_name, shard_table],
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _wait_for_ch_table_absence(
    connection: Any,
    table_name: str,
    timeout_seconds: int = 60,
    poll_interval_seconds: float = 1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = connection.query(f"EXISTS TABLE {table_name}")
        rows = getattr(result, "result_rows", None) or []
        if not rows or not rows[0] or not rows[0][0]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ClickHouse table {table_name} was still visible after "
                f"{timeout_seconds} second(s)."
            )
        time.sleep(poll_interval_seconds)


def _wait_for_ch_table_absence_on_cluster(
    connection: Any,
    table_name: str,
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    _wait_for_ch_tables_absence_on_cluster(
        connection,
        [table_name],
        ch_cluster=ch_cluster,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


def _wait_for_ch_tables_absence_on_cluster(
    connection: Any,
    table_names: Sequence[str],
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    normalized_table_names = [str(table_name).strip() for table_name in table_names]
    normalized_table_names = [
        table_name for table_name in normalized_table_names if table_name
    ]
    if not normalized_table_names:
        return

    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, one)"
    )

    deadline = time.monotonic() + timeout_seconds
    remote_hosts = 0
    expected_hosts = 0
    visible_table_rows: list[tuple[Any, ...]] = []
    last_error: Exception | None = None
    while True:
        try:
            remote_hosts, expected_hosts = _query_ch_cluster_host_counts(
                connection,
                cluster_name=cluster_name,
                remote_hosts_sql=expected_hosts_sql,
            )
            visible_table_rows = _query_ch_cluster_table_rows(
                connection,
                table_names=normalized_table_names,
                ch_cluster=cluster_name,
            )
            if (
                expected_hosts > 0
                and remote_hosts >= expected_hosts
                and not visible_table_rows
            ):
                return
        except Exception as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            table_summary = ", ".join(normalized_table_names)
            message = (
                f"ClickHouse table(s) {table_summary} were still visible on cluster "
                f"{cluster_name!r} after {timeout_seconds} second(s). Last "
                f"observed {len(visible_table_rows)} visible table row(s); reached "
                f"{remote_hosts}/{expected_hosts} expected host(s)."
            )
            leftovers = _format_ch_cluster_table_rows(visible_table_rows)
            if leftovers:
                message = f"{message} Leftover table(s): {leftovers}."
            message = (
                f"{message} Direct local drops on affected cluster hosts may "
                "be attempted with ch_retry_per_host_drops=True."
            )
            if last_error is not None:
                raise TimeoutError(message) from last_error
            raise TimeoutError(message)
        time.sleep(poll_interval_seconds)


def _wait_for_ch_table_on_cluster(
    connection: Any,
    table_name: str,
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(
        table_name
    )
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, one)"
    )
    visible_tables_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND name = {_sql_string_literal(relation_name)}"
    )

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            expected_hosts = _query_ch_expected_cluster_hosts(
                connection,
                cluster_name=cluster_name,
                remote_hosts_sql=expected_hosts_sql,
            )
            visible_tables = _query_ch_count(connection, visible_tables_sql)
            if expected_hosts > 0 and visible_tables >= expected_hosts:
                return
        except Exception as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            message = (
                f"ClickHouse table {table_name} was not visible on every "
                f"host in cluster {cluster_name!r} after {timeout_seconds} "
                "second(s)."
            )
            if last_error is not None:
                raise TimeoutError(message) from last_error
            raise TimeoutError(message)
        time.sleep(poll_interval_seconds)


def _wait_for_ch_table_schema_on_cluster(
    connection: Any,
    table_name: str,
    *,
    expected_column_types: Mapping[str, str],
    ch_cluster: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 1,
) -> None:
    expected_column_types = normalize_table_schema(
        expected_column_types,
        option_name="expected_column_types",
    )
    if not expected_column_types:
        return

    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(
        table_name
    )
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, one)"
    )
    matching_columns_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, columns)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND table = {_sql_string_literal(relation_name)}\n"
        f"  AND ({_build_ch_expected_schema_condition(expected_column_types)})"
    )

    deadline = time.monotonic() + timeout_seconds
    expected_hosts = 0
    matching_columns = 0
    last_error: Exception | None = None
    while True:
        try:
            expected_hosts = _query_ch_expected_cluster_hosts(
                connection,
                cluster_name=cluster_name,
                remote_hosts_sql=expected_hosts_sql,
            )
            matching_columns = _query_ch_count(connection, matching_columns_sql)
            expected_column_rows = expected_hosts * len(expected_column_types)
            if expected_hosts > 0 and matching_columns >= expected_column_rows:
                return
        except Exception as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            message = (
                f"ClickHouse table {table_name} schema did not match expected "
                f"columns on every host in cluster {cluster_name!r} after "
                f"{timeout_seconds} second(s). Last observed {matching_columns} "
                f"matching column row(s), expected "
                f"{expected_hosts * len(expected_column_types)}."
            )
            mismatch_details = _describe_ch_cluster_schema_mismatch(
                connection,
                table_name,
                expected_column_types=expected_column_types,
                ch_cluster=cluster_name,
                expected_hosts=expected_hosts,
            )
            if mismatch_details:
                message = f"{message} {mismatch_details}"
            if last_error is not None:
                raise TimeoutError(message) from last_error
            raise TimeoutError(message)
        time.sleep(poll_interval_seconds)


def _build_ch_expected_schema_condition(
    expected_column_types: Mapping[str, str],
) -> str:
    return " OR ".join(
        "("
        f"name = {_sql_string_literal(column_name)} "
        f"AND type = {_sql_string_literal(column_type)}"
        ")"
        for column_name, column_type in expected_column_types.items()
    )


def _describe_ch_cluster_schema_mismatch(
    connection: Any,
    table_name: str,
    *,
    expected_column_types: Mapping[str, str],
    ch_cluster: str,
    expected_hosts: int,
) -> str:
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(
        table_name
    )
    cluster_literal = _sql_string_literal(ch_cluster)
    observed_sql = (
        "SELECT name, type, count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, columns)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND table = {_sql_string_literal(relation_name)}\n"
        "GROUP BY name, type\n"
        "ORDER BY name, type"
    )
    try:
        observed_rows = _query_ch_rows(connection, observed_sql)
    except Exception:
        return ""

    observed: dict[str, dict[str, int]] = {}
    for row in observed_rows:
        if len(row) < 3:
            continue
        column_name, column_type, count = row[:3]
        observed.setdefault(str(column_name), {})[str(column_type)] = int(count)

    details: list[str] = []
    for column_name, expected_type in expected_column_types.items():
        type_counts = observed.get(column_name, {})
        if type_counts.get(expected_type, 0) == expected_hosts:
            continue
        if not type_counts:
            observed_summary = "missing"
        else:
            observed_summary = ", ".join(
                f"{column_type} on {count} host(s)"
                for column_type, count in sorted(type_counts.items())
            )
        details.append(
            f"{column_name}: expected {expected_type} on {expected_hosts} "
            f"host(s), observed {observed_summary}"
        )

    extra_columns = sorted(set(observed) - set(expected_column_types))
    if extra_columns:
        details.append("extra column(s): " + ", ".join(extra_columns[:5]))

    if not details:
        return ""
    if len(details) > 6:
        details = details[:6] + ["..."]
    return "Schema mismatch details: " + "; ".join(details)


def _resolve_ch_cluster_name_for_wait(connection: Any, cluster_name: str) -> str:
    unquoted = _strip_sql_wrapping_quotes(cluster_name)
    macro_name = _extract_ch_macro_name(unquoted)
    if macro_name is None:
        return unquoted

    try:
        result = connection.query(f"SELECT getMacro({_sql_string_literal(macro_name)})")
    except Exception as exc:
        raise ValueError(
            f"Could not resolve ClickHouse cluster macro {unquoted!r}. "
            "Pass ch_cluster with the concrete cluster name, for example "
            "ch_cluster='core'."
        ) from exc

    rows = getattr(result, "result_rows", None) or []
    if rows and rows[0] and rows[0][0] is not None:
        resolved = str(rows[0][0]).strip()
        if resolved:
            return resolved

    raise ValueError(
        f"Could not resolve ClickHouse cluster macro {unquoted!r}. "
        "Pass ch_cluster with the concrete cluster name, for example "
        "ch_cluster='core'."
    )


def _strip_sql_wrapping_quotes(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in {"'", '"', "`"}:
        return value
    inner = value[1:-1]
    if value[0] == "'":
        return inner.replace("''", "'")
    return inner


def _extract_ch_macro_name(value: str) -> str | None:
    match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
    if match is None:
        return None
    return match.group(1)


def _query_ch_count(connection: Any, sql: str) -> int:
    result = connection.query(sql)
    rows = getattr(result, "result_rows", None) or []
    if not rows:
        return 0
    return int(rows[0][0])


def _query_ch_cluster_table_rows(
    connection: Any,
    *,
    table_names: Sequence[str],
    ch_cluster: str,
) -> list[tuple[Any, ...]]:
    conditions: list[str] = []
    for table_name in table_names:
        database_expr, relation_name = split_ch_table_name_for_distributed_engine(
            table_name
        )
        conditions.append(
            f"(database = {database_expr} "
            f"AND name = {_sql_string_literal(relation_name)})"
        )
    if not conditions:
        return []

    cluster_literal = _sql_string_literal(ch_cluster)
    sql = (
        "SELECT hostName(), database, name, engine\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
        f"WHERE {' OR '.join(conditions)}\n"
        "ORDER BY hostName(), database, name"
    )
    return [row for row in _query_ch_rows(connection, sql) if len(row) >= 4]


def _format_ch_cluster_table_rows(rows: Sequence[Sequence[Any]]) -> str:
    formatted: list[str] = []
    for row in rows:
        if len(row) < 4:
            continue
        host, database, table_name, engine = row[:4]
        formatted.append(f"{host}: {database}.{table_name} ({engine})")
    if not formatted:
        return ""
    if len(formatted) > 10:
        formatted = formatted[:10] + ["..."]
    return "; ".join(formatted)


def _query_ch_expected_cluster_hosts(
    connection: Any,
    *,
    cluster_name: str,
    remote_hosts_sql: str,
) -> int:
    _, expected_hosts = _query_ch_cluster_host_counts(
        connection,
        cluster_name=cluster_name,
        remote_hosts_sql=remote_hosts_sql,
    )
    return expected_hosts


def _query_ch_cluster_host_counts(
    connection: Any,
    *,
    cluster_name: str,
    remote_hosts_sql: str,
) -> tuple[int, int]:
    remote_hosts = _query_ch_count(connection, remote_hosts_sql)
    configured_hosts_sql = (
        "SELECT count()\n"
        "FROM system.clusters\n"
        f"WHERE cluster = {_sql_string_literal(cluster_name)}"
    )
    try:
        configured_hosts = _query_ch_count(connection, configured_hosts_sql)
    except Exception:
        configured_hosts = 0
    return remote_hosts, max(remote_hosts, configured_hosts)


def _query_ch_rows(connection: Any, sql: str) -> list[tuple[Any, ...]]:
    result = connection.query(sql)
    return list(getattr(result, "result_rows", None) or [])

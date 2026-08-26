from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from analytics_toolkit.general import time_print
from analytics_toolkit.sql.connection.errors import SqlConfigError

from ...ddl.schema import normalize_table_schema
from .ddl import (
    _normalize_non_empty_string,
    _sql_string_literal,
    build_ch_shard_table_name,
    split_ch_table_name_for_distributed_engine,
)
from .readiness import run_ch_readiness_wait
from .routing import query_local
from .wait_policy import waits_for_distributed, waits_for_shard

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_MISSING_CLUSTER_SCOPE = object()
DEFAULT_DDL_READY_TIMEOUT_SECONDS = 300.0
_MAX_DIAGNOSTIC_HOSTS = 10


def after_create_table(
    adapter: Any,
    connection: Any,
    table_name: str,
    *,
    ch_cluster: str = "{cluster}",
    ch_distributed_table: bool = False,
    ch_only_shard: bool = False,
    expected_column_types: Mapping[str, str] | None = None,
    ch_creation_policy: Any = None,
) -> None:
    del adapter
    wait_policy = (
        getattr(ch_creation_policy, "ddl_wait_policy", "wait_all")
        if ch_creation_policy is not None
        else "wait_all"
    )
    if wait_policy == "wait_none":
        return
    shard_on_cluster = (
        ch_creation_policy.shard_on_cluster
        if ch_creation_policy is not None
        else ch_cluster
    )
    distributed_on_cluster = (
        ch_creation_policy.distributed_on_cluster
        if ch_creation_policy is not None
        else ch_cluster
    )
    routing_cluster = (
        ch_creation_policy.distributed_cluster
        if ch_creation_policy is not None
        else ch_cluster
    )
    timeout_seconds = (
        ch_creation_policy.ddl_ready_timeout_seconds
        if ch_creation_policy is not None
        else DEFAULT_DDL_READY_TIMEOUT_SECONDS
    )
    extension_cnt = (
        getattr(ch_creation_policy, "ddl_ready_timeout_extension_cnt", 0)
        if ch_creation_policy is not None
        else 0
    )
    timeout_increment = (
        getattr(ch_creation_policy, "ddl_ready_timeout_increment_seconds", 0.0)
        if ch_creation_policy is not None
        else 0.0
    )
    if not ch_distributed_table or ch_only_shard:
        if not waits_for_shard(wait_policy):
            return
        _wait_for_ch_physical_table(
            connection,
            table_name,
            shard_on_cluster=shard_on_cluster,
            timeout_seconds=timeout_seconds,
            expected_column_types=expected_column_types,
        )
        return
    run_ch_readiness_wait(
        lambda current_timeout: (
            _wait_for_ch_distributed_table_pair(
                connection,
                table_name,
                ch_cluster=ch_cluster,
                shard_on_cluster=shard_on_cluster,
                distributed_on_cluster=distributed_on_cluster,
                routing_cluster=routing_cluster,
                timeout_seconds=current_timeout,
                expected_column_types=expected_column_types,
                wait_policy=wait_policy,
            )
        ),
        timeout_seconds=timeout_seconds,
        extension_cnt=extension_cnt,
        timeout_increment_seconds=timeout_increment,
        wait_label="distributed-pair readiness",
    )


def _wait_for_ch_table(
    connection: Any,
    table_name: str,
    timeout_seconds: float = 60,
    poll_interval_seconds: float = 1,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = query_local(connection, f"EXISTS TABLE {table_name}")
        if result.result_rows and result.result_rows[0][0]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ClickHouse table {table_name} was not visible after {timeout_seconds} second(s)."
            )
        time.sleep(poll_interval_seconds)


def _wait_for_ch_distributed_table_pair(  # noqa: C901 - phases share one deadline.
    connection: Any,
    table_name: str,
    ch_cluster: str = "{cluster}",
    timeout_seconds: float = DEFAULT_DDL_READY_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 1,
    expected_column_types: Mapping[str, str] | None = None,
    shard_on_cluster: str | None | object = _MISSING_CLUSTER_SCOPE,
    distributed_on_cluster: str | None | object = _MISSING_CLUSTER_SCOPE,
    routing_cluster: str | None = None,
    wait_policy: str = "wait_all",
) -> None:
    shard_table = build_ch_shard_table_name(table_name)
    shard_scope = ch_cluster if shard_on_cluster is _MISSING_CLUSTER_SCOPE else shard_on_cluster
    distributed_scope = (
        ch_cluster
        if distributed_on_cluster is _MISSING_CLUSTER_SCOPE
        else distributed_on_cluster
    )
    routing_scope = routing_cluster
    deadline = time.monotonic() + timeout_seconds
    checks: list[tuple[str, Callable[[float], None]]] = []
    if waits_for_distributed(wait_policy):
        checks.append(
            (
                f"local distributed table {table_name}",
                lambda remaining: _wait_for_ch_table(
                    connection,
                    table_name,
                    timeout_seconds=remaining,
                    poll_interval_seconds=poll_interval_seconds,
                ),
            )
        )
    if waits_for_shard(wait_policy):
        checks.append(
            (
                f"local shard table {shard_table}",
                lambda remaining: _wait_for_ch_table(
                    connection,
                    shard_table,
                    timeout_seconds=remaining,
                    poll_interval_seconds=poll_interval_seconds,
                ),
            )
        )
    if waits_for_distributed(wait_policy) and distributed_scope is not None:
        checks.append(
            (
                f"distributed table {table_name} on cluster {distributed_scope!r}",
                lambda remaining: _wait_for_ch_table_on_cluster(
                    connection,
                    table_name,
                    ch_cluster=str(distributed_scope),
                    timeout_seconds=remaining,
                    poll_interval_seconds=poll_interval_seconds,
                ),
            )
        )
    if waits_for_shard(wait_policy) and shard_scope is not None:
        checks.append(
            (
                f"shard table {shard_table} on cluster {shard_scope!r}",
                lambda remaining: _wait_for_ch_table_on_cluster(
                    connection,
                    shard_table,
                    ch_cluster=str(shard_scope),
                    timeout_seconds=remaining,
                    poll_interval_seconds=poll_interval_seconds,
                ),
            )
        )
    if waits_for_shard(wait_policy) and routing_scope is not None:
        checks.append(
            (
                f"shard routing coverage on cluster {routing_scope!r}",
                lambda _remaining: _validate_ch_shard_routing_cluster(
                    connection,
                    shard_table,
                    ch_cluster=str(routing_scope),
                    shard_on_cluster=None if shard_scope is None else str(shard_scope),
                    expected_column_types=expected_column_types,
                ),
            )
        )
    if expected_column_types is not None:
        if waits_for_distributed(wait_policy) and distributed_scope is not None:
            checks.append(
                (
                    f"distributed table schema on cluster {distributed_scope!r}",
                    lambda remaining: _wait_for_ch_table_schema_on_cluster(
                        connection,
                        table_name,
                        expected_column_types=expected_column_types,
                        ch_cluster=str(distributed_scope),
                        timeout_seconds=remaining,
                        poll_interval_seconds=poll_interval_seconds,
                    ),
                )
            )
        if waits_for_shard(wait_policy) and shard_scope is not None:
            checks.append(
                (
                    f"shard table schema on cluster {shard_scope!r}",
                    lambda remaining: _wait_for_ch_table_schema_on_cluster(
                        connection,
                        shard_table,
                        expected_column_types=expected_column_types,
                        ch_cluster=str(shard_scope),
                        timeout_seconds=remaining,
                        poll_interval_seconds=poll_interval_seconds,
                    ),
                )
            )

    for phase, check in checks:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            check(remaining)
        except TimeoutError as exc:
            message = (
                f"ClickHouse distributed-pair readiness failed during {phase} "
                f"within the {timeout_seconds:g}-second deadline: {exc}"
            )
            time_print(message, level="warning", backend="ch", phase="validate_target")
            raise TimeoutError(message) from exc


def _wait_for_ch_physical_table(
    connection: Any,
    table_name: str,
    *,
    shard_on_cluster: str | None,
    timeout_seconds: float,
    expected_column_types: Mapping[str, str] | None,
) -> None:
    poll_interval_seconds = 1.0
    deadline = time.monotonic() + timeout_seconds
    _wait_for_ch_table(
        connection,
        table_name,
        timeout_seconds=max(0.0, deadline - time.monotonic()),
        poll_interval_seconds=poll_interval_seconds,
    )
    if shard_on_cluster is None:
        return
    _wait_for_ch_table_on_cluster(
        connection,
        table_name,
        ch_cluster=shard_on_cluster,
        timeout_seconds=max(0.0, deadline - time.monotonic()),
        poll_interval_seconds=poll_interval_seconds,
    )
    if expected_column_types is not None:
        _wait_for_ch_table_schema_on_cluster(
            connection,
            table_name,
            expected_column_types=expected_column_types,
            ch_cluster=shard_on_cluster,
            timeout_seconds=max(0.0, deadline - time.monotonic()),
            poll_interval_seconds=poll_interval_seconds,
        )


def _validate_ch_shard_routing_cluster(
    connection: Any,
    table_name: str,
    *,
    ch_cluster: str,
    shard_on_cluster: str | None,
    expected_column_types: Mapping[str, str] | None,
) -> None:
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(table_name)
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = f"SELECT count()\nFROM clusterAllReplicas({cluster_literal}, system, one)"
    visible_tables_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND name = {_sql_string_literal(relation_name)}"
    )
    expected_hosts = _query_ch_expected_cluster_hosts(
        connection,
        cluster_name=cluster_name,
        remote_hosts_sql=expected_hosts_sql,
    )
    visible_tables = _query_ch_count(connection, visible_tables_sql)
    if expected_hosts <= 0 or visible_tables < expected_hosts:
        details = _describe_ch_missing_routing_hosts(
            connection,
            table_name,
            ch_cluster=cluster_name,
        )
        message = (
            f"ClickHouse Distributed routing cluster {cluster_name!r} cannot use "
            f"shard table {table_name}: it is visible on {visible_tables}/{expected_hosts} "
            f"expected host(s), while shard DDL uses cluster {shard_on_cluster!r}. "
            "Set ddl_defaults.regular.distributed.cluster (or "
            "ch_distributed_cluster) to the cluster that contains the shard table."
        )
        if details:
            message = f"{message} {details}"
        raise SqlConfigError(message)

    if expected_column_types is None:
        return
    normalized_schema = normalize_table_schema(
        expected_column_types,
        option_name="expected_column_types",
    )
    if not normalized_schema:
        return
    matching_columns_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, columns)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND table = {_sql_string_literal(relation_name)}\n"
        f"  AND ({_build_ch_expected_schema_condition(normalized_schema)})"
    )
    matching_columns = _query_ch_count(connection, matching_columns_sql)
    expected_columns = expected_hosts * len(normalized_schema)
    if matching_columns >= expected_columns:
        return
    mismatch = _describe_ch_cluster_schema_mismatch(
        connection,
        table_name,
        expected_column_types=normalized_schema,
        ch_cluster=cluster_name,
        expected_hosts=expected_hosts,
    )
    message = (
        f"ClickHouse shard table {table_name} schema is not ready on Distributed "
        f"routing cluster {cluster_name!r}: observed {matching_columns}/{expected_columns} "
        "expected column row(s)."
    )
    if mismatch:
        message = f"{message} {mismatch}"
    raise SqlConfigError(message)


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
        result = query_local(connection, f"EXISTS TABLE {table_name}")
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
    normalized_table_names = [table_name for table_name in normalized_table_names if table_name]
    if not normalized_table_names:
        return

    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = f"SELECT count()\nFROM clusterAllReplicas({cluster_literal}, system, one)"

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
            if expected_hosts > 0 and remote_hosts >= expected_hosts and not visible_table_rows:
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
    timeout_seconds: float = 300,
    poll_interval_seconds: float = 1,
) -> None:
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(table_name)
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = f"SELECT count()\nFROM clusterAllReplicas({cluster_literal}, system, one)"
    visible_tables_sql = (
        "SELECT count()\n"
        f"FROM clusterAllReplicas({cluster_literal}, system, tables)\n"
        f"WHERE database = {database_expr}\n"
        f"  AND name = {_sql_string_literal(relation_name)}"
    )

    deadline = time.monotonic() + timeout_seconds
    expected_hosts = 0
    visible_tables = 0
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
                f"second(s). Last observed {visible_tables}/{expected_hosts} "
                "expected host table(s)."
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
    timeout_seconds: float = 300,
    poll_interval_seconds: float = 1,
) -> None:
    normalized_column_types = normalize_table_schema(
        expected_column_types,
        option_name="expected_column_types",
    )
    if not normalized_column_types:
        return
    expected_column_types = normalized_column_types

    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(table_name)
    cluster_literal = _sql_string_literal(cluster_name)
    expected_hosts_sql = f"SELECT count()\nFROM clusterAllReplicas({cluster_literal}, system, one)"
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
        "AND replaceRegexpAll(type, '\\\\s+', '') = "
        f"{_sql_string_literal(_normalize_ch_type_for_comparison(column_type))}"
        ")"
        for column_name, column_type in expected_column_types.items()
    )


def _normalize_ch_type_for_comparison(column_type: str) -> str:
    """Ignore formatting whitespace in ClickHouse's canonical type names."""
    return "".join(column_type.split())


def _describe_ch_cluster_schema_mismatch(
    connection: Any,
    table_name: str,
    *,
    expected_column_types: Mapping[str, str],
    ch_cluster: str,
    expected_hosts: int,
) -> str:
    database_expr, relation_name = split_ch_table_name_for_distributed_engine(table_name)
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
        normalized_expected = _normalize_ch_type_for_comparison(expected_type)
        matching_hosts = sum(
            count
            for observed_type, count in type_counts.items()
            if _normalize_ch_type_for_comparison(observed_type) == normalized_expected
        )
        if matching_hosts == expected_hosts:
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
        result = query_local(connection, f"SELECT getMacro({_sql_string_literal(macro_name)})")
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
    result = query_local(connection, sql)
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
        database_expr, relation_name = split_ch_table_name_for_distributed_engine(table_name)
        conditions.append(
            f"(database = {database_expr} AND name = {_sql_string_literal(relation_name)})"
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


def _describe_ch_missing_routing_hosts(
    connection: Any,
    table_name: str,
    *,
    ch_cluster: str,
) -> str:
    configured_hosts_sql = (
        "SELECT DISTINCT host_name\n"
        "FROM system.clusters\n"
        f"WHERE cluster = {_sql_string_literal(ch_cluster)}\n"
        "ORDER BY host_name"
    )
    configured_hosts = {
        str(row[0])
        for row in _query_ch_rows(connection, configured_hosts_sql)
        if row and row[0] is not None
    }
    visible_rows = _query_ch_cluster_table_rows(
        connection,
        table_names=[table_name],
        ch_cluster=ch_cluster,
    )
    visible_hosts = {str(row[0]) for row in visible_rows if row and row[0] is not None}
    missing_hosts = sorted(configured_hosts - visible_hosts)
    if not missing_hosts:
        return ""
    suffix = ", ..." if len(missing_hosts) > _MAX_DIAGNOSTIC_HOSTS else ""
    visible_names = ", ".join(missing_hosts[:_MAX_DIAGNOSTIC_HOSTS])
    return f"Missing routing host(s): {visible_names}{suffix}."


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
        f"SELECT count()\nFROM system.clusters\nWHERE cluster = {_sql_string_literal(cluster_name)}"
    )
    try:
        configured_hosts = _query_ch_count(connection, configured_hosts_sql)
    except Exception:
        configured_hosts = 0
    return remote_hosts, max(remote_hosts, configured_hosts)


def _query_ch_rows(connection: Any, sql: str) -> list[tuple[Any, ...]]:
    result = query_local(connection, sql)
    return list(getattr(result, "result_rows", None) or [])

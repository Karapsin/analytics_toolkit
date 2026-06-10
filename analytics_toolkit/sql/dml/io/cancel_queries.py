from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence, Union

import pandas as pd

from ...connection.config import get_connection_config
from ...execution.operation_runner import timed_public_sql_function
from .read_sql import read_sql


QueryIdInput = Union[int, str, Sequence[Union[int, str]]]

_GP_RUNNING_QUERY_IDS_SQL = """select pid as query_id
from pg_stat_activity
where usename = current_user
  and pid <> pg_backend_pid()"""
_TRINO_RUNNING_QUERY_IDS_SQL = """select query_id
from system.runtime.queries
where "user" = current_user
  and state in ('QUEUED', 'RUNNING')
  and query not like '%system.runtime.queries%'"""
_CH_RUNNING_QUERY_IDS_SQL = """select query_id
from system.processes
where user = currentUser()
  and query_id != currentQueryID()"""
_CANCEL_RESULT_COLUMNS = [
    "backend",
    "query_id",
    "cancel_query",
    "cancelled",
    "status",
]


@timed_public_sql_function
def cancel_queries(
    db_key: str,
    query_ids: QueryIdInput | None = None,
    *,
    cancel_all: bool = False,
    concurrency: int = 1,
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
) -> pd.DataFrame:
    _validate_concurrency(concurrency)
    config = get_connection_config(db_key)
    connection_key = config.connection_key
    backend = config.backend
    ids = _normalize_query_ids(query_ids, cancel_all=cancel_all)

    if cancel_all:
        ids = _running_query_ids(
            connection_key,
            backend,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        if not ids:
            return pd.DataFrame(columns=_CANCEL_RESULT_COLUMNS)

    def cancel_query(query_id: int | str) -> dict[str, Any]:
        normalized_id = _normalize_backend_query_id(backend, query_id)
        cancel_sql = _cancel_query_sql(backend, normalized_id)
        result = read_sql(
            connection_key,
            cancel_sql,
            print_queries=print_queries,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
            query_label=query_label,
        )
        cancelled, status = _cancel_status(backend, result)
        return {
            "backend": backend,
            "query_id": normalized_id,
            "cancel_query": cancel_sql,
            "cancelled": cancelled,
            "status": status,
        }

    if concurrency == 1:
        results = [cancel_query(query_id) for query_id in ids]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(cancel_query, ids))

    return pd.DataFrame(results, columns=_CANCEL_RESULT_COLUMNS)


def _running_query_ids(
    connection_key: str,
    backend: str,
    *,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> list[int | str]:
    query = _running_query_ids_sql(backend)
    rows = read_sql(
        connection_key,
        query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    if rows.empty:
        return []
    return [_normalize_backend_query_id(backend, value) for value in rows["query_id"]]


def _running_query_ids_sql(backend: str) -> str:
    if backend == "gp":
        return _GP_RUNNING_QUERY_IDS_SQL
    if backend == "trino":
        return _TRINO_RUNNING_QUERY_IDS_SQL
    if backend == "ch":
        return _CH_RUNNING_QUERY_IDS_SQL
    raise ValueError("Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'.")


def _cancel_query_sql(backend: str, query_id: int | str) -> str:
    normalized_id = _normalize_backend_query_id(backend, query_id)
    if backend == "gp":
        return f"select pg_cancel_backend({normalized_id}) as cancelled"
    if backend == "trino":
        return (
            "CALL system.runtime.kill_query("
            f"query_id => {_sql_string_literal(str(normalized_id))}, "
            "message => 'Cancelled by analytics_toolkit.cancel_queries')"
        )
    if backend == "ch":
        return (
            "KILL QUERY "
            f"WHERE query_id = {_sql_string_literal(str(normalized_id))} SYNC"
        )
    raise ValueError("Unsupported connection type. Expected one of: 'trino', 'gp', 'ch'.")


def _cancel_status(backend: str, result: pd.DataFrame) -> tuple[bool, str]:
    if backend == "gp":
        cancelled = bool(result["cancelled"].iloc[0])
        return cancelled, "cancelled" if cancelled else "not_cancelled"
    if backend == "ch" and "kill_status" in result.columns and not result.empty:
        statuses = [str(value) for value in result["kill_status"].tolist()]
        return all(status == "finished" for status in statuses), ", ".join(statuses)
    return True, "submitted"


def _normalize_query_ids(
    query_ids: QueryIdInput | None,
    *,
    cancel_all: bool,
) -> list[int | str]:
    if query_ids is None and not cancel_all:
        raise ValueError("Provide query_ids or set cancel_all=True.")
    if query_ids is not None and cancel_all:
        raise ValueError("Provide query_ids or cancel_all=True, not both.")
    if query_ids is None:
        return []
    if isinstance(query_ids, (str, int)) and not isinstance(query_ids, bool):
        return [query_ids]
    if isinstance(query_ids, SequenceABC):
        ids = list(query_ids)
        if not ids:
            raise ValueError("query_ids must not be empty.")
        if any(isinstance(query_id, bool) for query_id in ids):
            raise ValueError("query_ids must contain strings or integers.")
        if not all(isinstance(query_id, (str, int)) for query_id in ids):
            raise ValueError("query_ids must contain strings or integers.")
        return ids
    raise ValueError("query_ids must be a string, integer, or sequence of those values.")


def _normalize_backend_query_id(backend: str, query_id: Any) -> int | str:
    if backend == "gp":
        if isinstance(query_id, bool):
            raise ValueError("Greenplum query_ids must be backend PIDs.")
        try:
            return int(query_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Greenplum query_ids must be backend PIDs.") from exc
    if isinstance(query_id, str):
        normalized = query_id.strip()
        if not normalized:
            raise ValueError("query_ids must not contain empty strings.")
        return normalized
    if isinstance(query_id, int) and not isinstance(query_id, bool):
        return str(query_id)
    raise ValueError("query_ids must contain strings or integers.")


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _validate_concurrency(concurrency: int) -> None:
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
    ):
        raise ValueError("concurrency must be an integer >= 1.")

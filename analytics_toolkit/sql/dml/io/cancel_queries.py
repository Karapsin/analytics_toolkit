from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence, Union

import pandas as pd

from ...backends import get_backend_adapter
from ...connection.config import get_connection_config
from ...execution.operation_runner import timed_public_sql_function, validate_retry_options
from ...metadata.show_queries import show_queries
from .read_sql import read_sql


QueryIdInput = Union[int, str, Sequence[Union[int, str]]]

_CANCEL_RESULT_COLUMNS = [
    "backend",
    "query_id",
    "cancel_query",
    "cancelled",
    "terminated",
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
    validate_retry_options(retry_cnt, timeout_increment)
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
        cancel_result = _cancel_result(backend, result)
        return {
            "backend": backend,
            "query_id": normalized_id,
            "cancel_query": cancel_sql,
            **cancel_result,
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
    rows = show_queries(
        connection_key,
        state="active",
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    if rows.empty:
        return []
    return [_normalize_backend_query_id(backend, value) for value in rows["query_id"]]


def _running_query_ids_sql(backend: str) -> str:
    return get_backend_adapter(backend).running_query_ids_sql()


def _cancel_query_sql(backend: str, query_id: int | str) -> str:
    return get_backend_adapter(backend).cancel_query_sql(query_id)


def _cancel_result(backend: str, result: pd.DataFrame) -> dict[str, Any]:
    return get_backend_adapter(backend).cancel_result(result)


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
    return get_backend_adapter(backend).normalize_query_id(query_id)


def _validate_concurrency(concurrency: int) -> None:
    if concurrency.__class__ is not int or concurrency < 1:
        raise ValueError("concurrency must be an integer >= 1.")

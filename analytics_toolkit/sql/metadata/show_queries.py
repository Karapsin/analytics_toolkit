from __future__ import annotations

from collections.abc import Sequence as SequenceABC
import warnings

import pandas as pd

from ..backend_adapters import get_backend_adapter
from ..connection.config import get_connection_config
from ..execution.operation_runner import timed_public_sql_function


QUERY_STATE_VALUES = {"active", "finished", "failed"}
SHOW_QUERIES_COLUMNS = [
    "backend",
    "query_id",
    "user",
    "state",
    "query",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "source",
    "database",
    "raw_state",
]


@timed_public_sql_function
def show_queries(
    db_key: str,
    *,
    user: str | None = None,
    state: str | SequenceABC[str] = "active",
    print_queries: bool = False,
    retry_cnt: int = 5,
    timeout_increment: int | float = 5,
    query_label: str | None = None,
) -> pd.DataFrame:
    config = get_connection_config(db_key)
    states = normalize_query_states(state)
    frames: list[pd.DataFrame] = []

    for query in get_backend_adapter(config.backend).show_queries_sqls(
        user=user,
        states=states,
    ):
        if query.get("unsupported_states"):
            _warn_unsupported_history(
                config.backend,
                list(query["unsupported_states"]),
            )
            continue
        try:
            frames.append(
                _read_queries(
                    config.connection_key,
                    config.backend,
                    str(query["sql"]),
                    print_queries=print_queries,
                    retry_cnt=retry_cnt,
                    timeout_increment=timeout_increment,
                    query_label=query_label,
                )
            )
        except Exception as exc:
            if query.get("history"):
                warnings.warn(
                    "Could not read historical query records for "
                    f"{config.backend}: {exc!r}",
                    stacklevel=2,
                )
                continue
            raise

    if not frames:
        return _empty_queries_frame()

    non_empty = [frame for frame in frames if not frame.empty]
    if not non_empty:
        return _empty_queries_frame()
    return pd.concat(non_empty, ignore_index=True)[SHOW_QUERIES_COLUMNS]


def normalize_query_states(state: str | SequenceABC[str]) -> list[str]:
    if isinstance(state, str):
        raw_states = [state]
    elif isinstance(state, SequenceABC):
        raw_states = list(state)
    else:
        raise ValueError("state must be a string or a sequence of strings.")

    if not raw_states:
        raise ValueError("state must not be empty.")

    normalized: list[str] = []
    for raw_state in raw_states:
        if not isinstance(raw_state, str):
            raise ValueError("state entries must be strings.")
        value = raw_state.strip().lower()
        if value == "all":
            values = ["active", "finished", "failed"]
        elif value in QUERY_STATE_VALUES:
            values = [value]
        else:
            raise ValueError("state must contain active, finished, failed, or all.")
        for item in values:
            if item not in normalized:
                normalized.append(item)
    return normalized


def _read_queries(
    connection_key: str,
    backend: str,
    query: str,
    *,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    result = _execute_read_sql(
        connection_key,
        query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )
    return _normalize_result(result, backend)


def _execute_read_sql(
    connection_key: str,
    query: str,
    *,
    print_queries: bool,
    retry_cnt: int,
    timeout_increment: int | float,
    query_label: str | None,
) -> pd.DataFrame:
    from ..dml.io.read_sql import read_sql

    return read_sql(
        connection_key,
        query,
        print_queries=print_queries,
        retry_cnt=retry_cnt,
        timeout_increment=timeout_increment,
        query_label=query_label,
    )


def _normalize_result(result: pd.DataFrame, backend: str) -> pd.DataFrame:
    frame = result.copy()
    if "backend" not in frame.columns:
        frame["backend"] = backend
    for column in SHOW_QUERIES_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame[SHOW_QUERIES_COLUMNS]


def _empty_queries_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=SHOW_QUERIES_COLUMNS)


def _warn_unsupported_history(backend: str, states: list[str]) -> None:
    warnings.warn(
        f"{backend} does not expose historical query states via show_queries: "
        + ", ".join(states),
        stacklevel=3,
    )


__all__ = ["SHOW_QUERIES_COLUMNS", "normalize_query_states", "show_queries"]

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest


load_table = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")
gp_insert = importlib.import_module("analytics_toolkit.sql.backends.gp.insert")


class LoadAdapter:
    backend = "gp"

    def __init__(self, *, ambiguous: bool = False, refresh: bool = False) -> None:
        self.ambiguous = ambiguous
        self.refresh = refresh
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def normalize_insert_batch(self, batch: pd.DataFrame) -> pd.DataFrame:
        self.calls.append(("normalize_insert_batch", (batch,), {}))
        return batch.copy()

    def normalize_insert_rows(self, rows: Any) -> list[tuple[Any, ...]]:
        self.calls.append(("normalize_insert_rows", (rows,), {}))
        return [tuple(row) for row in rows]

    def should_wrap_insert_error_as_ambiguous(
        self,
        connection: Any,
        exc: Exception,
    ) -> bool:
        self.calls.append(("should_wrap", (connection, exc), {}))
        return self.ambiguous

    def should_refresh_connection_before_insert_retry(self) -> bool:
        self.calls.append(("should_refresh", (), {}))
        return self.refresh

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            results = {
                "build_dataframe_batch_insert_sql": "INSERT values",
                "normalize_insert_batch": args[0] if args else None,
                "normalize_insert_rows": list(args[0]) if args else [],
            }
            return results.get(name)

        return method


def _run_once(**kwargs: Any) -> Any:
    return kwargs["operation"](1)


def test_execute_values_delegates_to_greenplum_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        gp_insert,
        "execute_values",
        lambda *args: calls.append(args) or "result",
    )

    assert load_table.execute_values("cursor", "INSERT", [(1,)], 10) == "result"
    assert calls == [("cursor", "INSERT", [(1,)], 10)]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_insert_table_batch_classifies_insert_failures(
    monkeypatch: pytest.MonkeyPatch,
    ambiguous: bool,
) -> None:
    adapter = LoadAdapter(ambiguous=ambiguous)
    logs: list[str] = []
    monkeypatch.setattr(load_table, "resolve_connection_backend", lambda _value: "gp")
    monkeypatch.setattr(load_table, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(load_table, "time_print", lambda text, **_kwargs: logs.append(text))

    def fail_insert(*_args: Any, **_kwargs: Any) -> None:
        message = "write failed"
        raise RuntimeError(message)

    monkeypatch.setattr(load_table, "_insert_batch_backend", fail_insert)

    expected_error = load_table.AmbiguousTableLoadError if ambiguous else RuntimeError
    with pytest.raises(expected_error):
        load_table.insert_table_batch(
            "gp",
            {"connection": object()},
            "schema.stage",
            pd.DataFrame({"id": [1]}),
            _run_once,
            retry_cnt=1,
            timeout_increment=0,
        )

    assert (len(logs) == 2) is ambiguous


def test_insert_rows_batch_returns_zero_without_retry_for_empty_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LoadAdapter()
    monkeypatch.setattr(load_table, "resolve_connection_backend", lambda value: value)
    monkeypatch.setattr(load_table, "get_backend_adapter", lambda _backend: adapter)

    assert (
        load_table.insert_rows_batch(
            "gp",
            {"connection": object()},
            "stage",
            ["id"],
            [],
            lambda **_kwargs: pytest.fail("retry must not run"),
            retry_cnt=1,
            timeout_increment=0,
        )
        == 0
    )
    assert adapter.calls == []


@pytest.mark.parametrize("ambiguous", [False, True])
def test_insert_rows_batch_classifies_insert_failures(
    monkeypatch: pytest.MonkeyPatch,
    ambiguous: bool,
) -> None:
    adapter = LoadAdapter(ambiguous=ambiguous)
    logs: list[str] = []
    monkeypatch.setattr(load_table, "resolve_connection_backend", lambda value: value)
    monkeypatch.setattr(load_table, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(load_table, "time_print", lambda text, **_kwargs: logs.append(text))

    def fail_insert(*_args: Any, **_kwargs: Any) -> None:
        message = "row write failed"
        raise RuntimeError(message)

    monkeypatch.setattr(load_table, "_insert_rows_backend", fail_insert)

    expected_error = load_table.AmbiguousTableLoadError if ambiguous else RuntimeError
    with pytest.raises(expected_error):
        load_table.insert_rows_batch(
            "gp",
            {"connection": object()},
            "stage",
            ["id"],
            [(1,)],
            _run_once,
            retry_cnt=1,
            timeout_increment=0,
        )
    assert (len(logs) == 2) is ambiguous


@pytest.mark.parametrize(
    ("refresh", "attempt", "retry_cnt", "key", "has_replace", "expected"),
    [
        (False, 0, 2, "alias", True, False),
        (True, 2, 2, "alias", True, False),
        (True, 0, 2, None, True, False),
        (True, 0, 2, "alias", False, False),
        (True, 0, 2, "alias", True, True),
    ],
)
def test_replace_connection_before_retry_respects_refresh_policy(
    refresh: bool,
    attempt: int,
    retry_cnt: int,
    key: str | None,
    has_replace: bool,
    expected: bool,
) -> None:
    adapter = LoadAdapter(refresh=refresh)
    connection_ref = {"connection": object()}
    rollbacks: list[Any] = []
    replacements: list[tuple[str, dict[str, Any]]] = []

    load_table._replace_connection_before_next_insert_retry(
        adapter=adapter,
        connection_key=key,
        connection_ref=connection_ref,
        rollback_fn=rollbacks.append,
        replace_connection_fn=(
            (lambda connection_key, ref: replacements.append((connection_key, ref)))
            if has_replace
            else None
        ),
        attempt=attempt,
        retry_cnt=retry_cnt,
    )

    assert bool(replacements) is expected
    assert bool(rollbacks) is expected


def test_replace_connection_before_retry_allows_missing_rollback() -> None:
    adapter = LoadAdapter(refresh=True)
    replacements: list[str] = []

    load_table._replace_connection_before_next_insert_retry(
        adapter=adapter,
        connection_key="alias",
        connection_ref={"connection": object()},
        rollback_fn=None,
        replace_connection_fn=lambda key, _ref: replacements.append(key),
        attempt=0,
        retry_cnt=1,
    )

    assert replacements == ["alias"]


def test_replace_connection_before_retry_skips_non_retryable_error() -> None:
    replacements: list[str] = []
    rollbacks: list[Any] = []

    load_table._replace_connection_before_next_insert_retry(
        adapter=LoadAdapter(refresh=True),
        connection_key="alias",
        connection_ref={"connection": object()},
        rollback_fn=rollbacks.append,
        replace_connection_fn=lambda key, _ref: replacements.append(key),
        attempt=1,
        retry_cnt=5,
        error=RuntimeError("can't adapt type 'UUID'"),
    )

    assert replacements == []
    assert rollbacks == []


def test_load_table_normalization_and_adapter_insert_compatibility_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LoadAdapter()
    monkeypatch.setattr(load_table, "get_backend_adapter", lambda _backend: adapter)
    batch = pd.DataFrame({"id": [1]})
    progress: list[int] = []

    assert load_table.normalize_batch(batch).equals(batch)
    assert load_table.normalize_rows([[1], [2]]) == [(1,), (2,)]
    load_table._insert_gp_batch("connection", "target", batch, 10, "q", progress.append)
    load_table._insert_gp_rows(
        "connection",
        "target",
        ["id"],
        [(1,)],
        10,
        "q",
        progress.append,
        lambda: 5,
        lambda *_args: None,
    )
    load_table._insert_trino_batch(
        "connection",
        "target",
        batch,
        {"id": "bigint"},
        10,
        "alias",
        "q",
        progress.append,
    )
    load_table._insert_trino_rows(
        "connection",
        "target",
        ["id"],
        [(1,)],
        {"id": "bigint"},
        10,
        "alias",
        "q",
        progress.append,
    )
    load_table._insert_ch_batch("connection", "target", batch, progress.append)
    load_table._insert_ch_rows(
        "connection", "target", ["id"], [(1,)], {"id": "Int64"}, progress.append
    )

    names = [call[0] for call in adapter.calls]
    assert names == [
        "normalize_insert_batch",
        "normalize_insert_rows",
        "_insert_dataframe_batch",
        "_insert_rows",
        "_insert_dataframe_batch",
        "_insert_rows",
        "_insert_dataframe_batch",
        "_insert_rows",
    ]


def test_load_table_backend_insert_delegates(adapter: LoadAdapter | None = None) -> None:
    del adapter


def test_insert_backend_helpers_forward_all_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LoadAdapter()
    monkeypatch.setattr(load_table, "get_backend_adapter", lambda _backend: adapter)
    batch = pd.DataFrame({"id": [1]})

    load_table._insert_batch_backend(
        "gp",
        "connection",
        "target",
        batch,
        target_column_types={"id": "bigint"},
        trino_insert_chunk_size=10,
        gp_insert_chunk_size=20,
        connection_type="alias",
        query_label="q",
        on_progress=None,
    )
    load_table._insert_rows_backend(
        "gp",
        "connection",
        "target",
        ["id"],
        [(1,)],
        target_column_types={"id": "bigint"},
        trino_insert_chunk_size=10,
        gp_insert_chunk_size=20,
        connection_type="alias",
        query_label="q",
        on_progress=None,
    )

    assert [call[0] for call in adapter.calls] == [
        "insert_dataframe_batch",
        "insert_rows_batch",
    ]


def test_load_table_sql_builders_and_positive_row_count(adapter: Any = None) -> None:
    del adapter


def test_load_table_sql_builders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LoadAdapter()
    monkeypatch.setattr(load_table, "get_backend_adapter", lambda _backend: adapter)

    assert load_table.build_gp_batch_insert_sql("target", ["id"], "q") == ("INSERT values")
    assert load_table.build_trino_batch_insert_sql("target", ["id"], 2, "q") == ("INSERT values")
    with pytest.raises(ValueError, match="positive integer"):
        load_table.build_trino_batch_insert_sql("target", ["id"], 0)


def test_load_table_scalar_row_and_chunk_compatibility_helpers() -> None:
    batch = pd.DataFrame({"id": ["1", "2"], "value": [3, None]})

    normalized_batch = load_table.normalize_ch_batch(batch)
    assert list(normalized_batch.columns) == ["id", "value"]
    assert load_table._normalize_ch_row([pd.NA, 1]) == (None, 1)
    assert load_table._normalize_ch_scalar(pd.NA) is pd.NA
    assert list(
        load_table._iter_trino_rows(
            batch,
            {"id": "bigint", "value": "varchar"},
        )
    ) == [(1, "3.0"), (2, None)]
    assert list(
        load_table._iter_trino_row_values(
            ["id"],
            [("3",)],
            {"id": "bigint"},
        )
    ) == [(3,)]
    assert load_table._normalize_trino_value("4", "bigint") == 4
    assert load_table._build_trino_values_tuple(["id"], [1], {"id": "bigint"}) == "(1)"
    load_table._validate_row_width(["id"], [1])
    assert list(load_table._chunk_rows(iter([(1,), (2,), (3,)]), 2)) == [
        [(1,), (2,)],
        [(3,)],
    ]
    assert list(load_table._chunk_sequence([(1,), (2,), (3,)], 2)) == [
        [(1,), (2,)],
        [(3,)],
    ]
    assert load_table._trino_literal("O'Reilly", None) == "'O''Reilly'"
    assert load_table._get_trino_insert_chunk_size(7) == 7
    assert load_table._get_gp_insert_chunk_size(8) == 8
    assert load_table._column_type_names(["id", "value"], {"id": "Int64", "value": "String"}) == [
        "Int64",
        "String",
    ]

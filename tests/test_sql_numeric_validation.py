from __future__ import annotations

import importlib
from typing import Any

import pandas as pd
import pytest

operation_runner = importlib.import_module(
    "analytics_toolkit.sql.execution.operation_runner"
)
transfer_api = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.api"
)
load_df_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_df")
drop_tables_module = importlib.import_module(
    "analytics_toolkit.sql.dml.table.drop_tables"
)
staging_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.staging"
)


@pytest.mark.parametrize("retry_cnt", [True, 1.5, "2", None])
def test_retry_count_requires_builtin_positive_integer(retry_cnt: Any) -> None:
    with pytest.raises(ValueError, match="retry_cnt"):
        operation_runner.validate_retry_options(retry_cnt, 0)


@pytest.mark.parametrize(
    "timeout_increment",
    [True, "1", -1, float("nan"), float("inf")],
)
def test_retry_delay_requires_finite_non_negative_number(
    timeout_increment: Any,
) -> None:
    with pytest.raises(ValueError, match="timeout_increment"):
        operation_runner.validate_retry_options(1, timeout_increment)


@pytest.mark.parametrize(
    ("overrides", "option_name"),
    [
        ({"batch_size": 1.5}, "batch_size"),
        ({"min_batch_size": True}, "min_batch_size"),
        ({"max_batch_size": 10.5}, "max_batch_size"),
        ({"target_batch_seconds": float("nan")}, "target_batch_seconds"),
        ({"target_batch_memory_mb": float("inf")}, "target_batch_memory_mb"),
        ({"retry_cnt": True}, "retry_cnt"),
        ({"timeout_increment": float("nan")}, "timeout_increment"),
        ({"full_retry_cnt": 1.5}, "full_retry_cnt"),
        ({"full_timeout_increment": float("inf")}, "full_timeout_increment"),
        ({"gp_insert_chunk_size": True}, "gp_insert_chunk_size"),
        ({"trino_insert_chunk_size": 1.5}, "trino_insert_chunk_size"),
        ({"concurrency": True}, "concurrency"),
    ],
)
def test_transfer_rejects_invalid_numeric_options_before_config_lookup(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    option_name: str,
) -> None:
    monkeypatch.setattr(
        transfer_api,
        "get_connection_config",
        lambda key: pytest.fail(f"config lookup should not run for {key}"),
    )

    with pytest.raises(ValueError, match=option_name):
        transfer_api.build_transfer_options(
            from_db="source",
            to_db="target",
            from_sql="select 1",
            to_table="sandbox.target",
            **overrides,
        )


@pytest.mark.parametrize(
    ("option_name", "value"),
    [
        ("gp_insert_chunk_size", True),
        ("gp_insert_chunk_size", 1.5),
        ("trino_insert_chunk_size", True),
        ("trino_insert_chunk_size", 1.5),
    ],
)
def test_load_df_rejects_invalid_chunk_size_before_option_build(
    monkeypatch: pytest.MonkeyPatch,
    option_name: str,
    value: Any,
) -> None:
    monkeypatch.setattr(
        load_df_module,
        "_build_load_options",
        lambda **kwargs: pytest.fail("load options should not be built"),
    )

    with pytest.raises(ValueError, match=option_name):
        load_df_module.load_df(
            "gp",
            "sandbox.target",
            pd.DataFrame({"id": [1]}),
            **{option_name: value},
        )


@pytest.mark.parametrize(
    ("overrides", "option_name"),
    [
        ({"ch_wait_timeout_seconds": True}, "ch_wait_timeout_seconds"),
        ({"ch_wait_timeout_seconds": 1.5}, "ch_wait_timeout_seconds"),
        (
            {"ch_wait_poll_interval_seconds": float("nan")},
            "ch_wait_poll_interval_seconds",
        ),
        (
            {"ch_wait_poll_interval_seconds": float("inf")},
            "ch_wait_poll_interval_seconds",
        ),
    ],
)
def test_drop_tables_rejects_invalid_wait_options_before_config_lookup(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    option_name: str,
) -> None:
    monkeypatch.setattr(
        drop_tables_module,
        "get_connection_config",
        lambda key: pytest.fail(f"config lookup should not run for {key}"),
    )

    with pytest.raises(ValueError, match=option_name):
        drop_tables_module.drop_tables("ch", "sandbox.target", **overrides)


@pytest.mark.parametrize("read_retry_cnt", [True, 1.5, 0])
def test_cleanup_rejects_invalid_read_retry_before_opening_connection(
    monkeypatch: pytest.MonkeyPatch,
    read_retry_cnt: Any,
) -> None:
    monkeypatch.setattr(
        staging_module,
        "get_sql_connection",
        lambda key: pytest.fail(f"connection should not open for {key}"),
    )

    with pytest.raises(ValueError, match="read_retry_cnt"):
        staging_module.cleanup_stale_stage_tables(
            "gp",
            "sandbox.target",
            read_retry_cnt=read_retry_cnt,
        )

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.retry_flow import (
    FailingSourceConnection,
    SourceConversionError,
    arrange_stage_insert_failure,
    arrange_transfer_attempt,
    record_source_replacements,
    terminal_insert_error,
)
from tests.sql._support.row_batches import load_sql_table_module, retry_module, source_module


@pytest.mark.parametrize("backend", ["ch", "trino"])
@pytest.mark.parametrize("dataframe", [False, True])
def test_transfer_does_not_restart_terminal_stage_insert(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    dataframe: bool,
) -> None:
    error = terminal_insert_error(backend)
    inserts = arrange_stage_insert_failure(monkeypatch, error, dataframe=dataframe)
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def transfer_attempt(**kwargs: Any) -> int:
        attempts.append(1)
        options = kwargs["options"]
        insert_options = {
            "retry_fn": retry_module.run_with_retry,
            "retry_cnt": kwargs["insert_retry_cnt"],
            "timeout_increment": options.timeout_increment,
        }
        if dataframe:
            return load_sql_table_module.insert_table_batch(
                backend,
                {"connection": object()},
                "sandbox.stage",
                pd.DataFrame({"id": [1]}),
                **insert_options,
            )
        return load_sql_table_module.insert_rows_batch(
            backend,
            {"connection": object()},
            "sandbox.stage",
            ["id"],
            [(1,)],
            **insert_options,
        )

    arrange_transfer_attempt(monkeypatch, transfer_attempt, target_backend=backend)

    with pytest.raises(type(error)) as caught:
        sql.transfer("gp", backend)

    assert caught.value is error
    assert attempts == [1]
    assert inserts == [backend]
    assert sleeps == []
    assert getattr(error, "analytics_toolkit_sql_retry_safe", True) is False
    assert getattr(error, "analytics_toolkit_sql_backend", None) == backend


def test_transfer_preserves_terminal_source_error_through_cleanup_and_target_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SourceConversionError("malformed source value")
    connection = FailingSourceConnection(error)
    replacements = record_source_replacements(monkeypatch)
    attempts: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def transfer_attempt(**_kwargs: Any) -> int:
        attempts.append(1)
        return len(
            list(
                source_module.iter_source_batches(
                    "gp",
                    "gp",
                    {"connection": connection},
                    "SELECT id FROM source",
                    batch_size=1,
                    retry_cnt=3,
                    timeout_increment=5,
                )
            )
        )

    arrange_transfer_attempt(monkeypatch, transfer_attempt, target_backend="ch")

    with pytest.raises(SourceConversionError) as caught:
        sql.transfer("gp", "ch")

    assert caught.value is error
    assert attempts == [1]
    assert connection.cursor_obj.execute_calls == 1
    assert connection.cursor_obj.close_calls == 1
    assert connection.rollback_calls == 1
    assert replacements == []
    assert sleeps == []
    assert getattr(error, "analytics_toolkit_sql_backend", None) == "gp"

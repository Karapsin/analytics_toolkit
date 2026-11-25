from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd
from analytics_toolkit import sql

from tests.sql._support.retry_flow import arrange_transfer_attempt
from tests.sql._support.row_batches import (
    FakeTransferConnection,
    RecordingSourceConnection,
    load_sql_table_module,
    retry_module,
    source_module,
)

if TYPE_CHECKING:
    import pytest


def test_transfer_dataframe_insert_recovers_on_fresh_gp_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_connection = FakeTransferConnection("first")
    replacement_connection = FakeTransferConnection("replacement")
    connection_ref = {"connection": first_connection}
    insert_connections: list[FakeTransferConnection] = []
    replacements: list[str] = []
    transfer_attempts: list[int] = []
    sleeps: list[float] = []

    def insert(_backend: str, connection: FakeTransferConnection, *_a: Any, **_k: Any) -> None:
        insert_connections.append(connection)
        if connection is first_connection:
            message = "temporary connection loss"
            raise ConnectionError(message)

    def replace_connection(key: str, ref: dict[str, Any]) -> None:
        replacements.append(key)
        ref["connection"].close()
        ref["connection"] = replacement_connection

    def transfer_attempt(**kwargs: Any) -> int:
        transfer_attempts.append(1)
        return load_sql_table_module.insert_table_batch(
            "gp",
            connection_ref,
            "sandbox.stage",
            pd.DataFrame({"id": [1]}),
            retry_fn=retry_module.run_with_retry,
            retry_cnt=kwargs["insert_retry_cnt"],
            timeout_increment=5,
            connection_key="gp_sandbox",
            rollback_fn=retry_module.rollback_quietly,
            replace_connection_fn=replace_connection,
        )

    monkeypatch.setattr(load_sql_table_module, "_insert_batch_backend", insert)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)
    arrange_transfer_attempt(
        monkeypatch, transfer_attempt, target_backend="gp", target_key="gp_sandbox"
    )

    assert sql.transfer("gp", "gp_sandbox") == 1
    assert insert_connections == [first_connection, replacement_connection]
    assert replacements == ["gp_sandbox"]
    assert first_connection.close_calls == 1
    assert transfer_attempts == [1]
    assert sleeps == [5]


def test_transfer_keeps_completed_source_rows_when_cursor_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = RecordingSourceConnection([(1,)])
    closes: list[int] = []
    transfer_attempts: list[int] = []

    def close_cursor() -> None:
        closes.append(1)
        message = "private cleanup credentials"
        raise OSError(message)

    def transfer_attempt(**_kwargs: Any) -> int:
        transfer_attempts.append(1)
        return sum(
            batch.row_count
            for batch in source_module.iter_source_batches(
                "gp",
                "gp",
                {"connection": connection},
                "SELECT id FROM source",
                batch_size=1,
                retry_cnt=3,
                timeout_increment=0,
            )
        )

    monkeypatch.setattr(connection.cursor_obj, "close", close_cursor)
    arrange_transfer_attempt(monkeypatch, transfer_attempt, target_backend="ch")

    assert sql.transfer("gp", "ch") == 1
    assert transfer_attempts == [1]
    assert connection.cursor_obj.executed == ["SELECT id FROM source"]
    assert closes == [1]
    output = capsys.readouterr().out
    assert "SQL close_source_cursor also failed: OSError" in output
    assert "private cleanup credentials" not in output

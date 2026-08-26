from __future__ import annotations

from tests.sql._support.backend_helpers import (
    Any,
    RecordingConnection,
    RecordingCursor,
    gp_adapter_module,
    importlib,
    pd,
    pytest,
)


def test_greenplum_execute_loops_read_cleanup_and_dataframe_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = gp_adapter_module.GreenplumAdapter()
    messages: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    connection = RecordingConnection()
    adapter.execute_sql(
        connection,
        "SELECT 1; SELECT 2",
        print_queries=False,
        gp_break_query=True,
        gp_commit_each_statement=False,
        progress=False,
    )
    assert connection.commits == 1

    failed_cursor = RecordingCursor(fail_on="SET broken")
    failed_connection = RecordingConnection(failed_cursor)
    with pytest.raises(RuntimeError, match="query failed"):
        adapter.execute_read_sql(
            failed_connection,
            ["SET broken", "SELECT 1"],
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert failed_cursor.closed is True
    assert "Failed SQL:\nSELECT 1" in messages

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        adapter,
        "_insert_rows",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )
    adapter._insert_dataframe_batch(
        object(),
        "target",
        pd.DataFrame({"id": [1], "value": [None]}),
        gp_insert_chunk_size=3,
        query_label="batch",
        on_progress=None,
    )
    assert list(calls[0][2]) == ["id", "value"]
    assert calls[0][3] == [(1, None)]

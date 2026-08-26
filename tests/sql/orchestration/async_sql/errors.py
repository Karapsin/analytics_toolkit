from __future__ import annotations

from tests.sql._support.async_sql import (
    Any,
    async_module,
    pd,
    pytest,
    threading,
    time,
    time_print,
)


def test_async_sql_fail_fast_false_prints_failed_task_query(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.async_sql(
        [
            {
                "name": "broken",
                "type": "read",
                "db_key": "gp",
                "query": "select * from broken_table",
            }
        ],
        fail_fast=False,
        progress=False,
    )

    assert result["broken"] == str(error)
    output = capsys.readouterr().out
    assert "SQL task 'broken' (read) failed while running query" in output
    assert "query:\nselect * from broken_table" in output


def test_async_sql_fail_fast_true_prints_failed_transfer_sql(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("transfer failed")

    def fake_transfer_table(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(async_module, "transfer_table", fake_transfer_table)

    with pytest.raises(RuntimeError) as exc_info:
        async_module.async_sql(
            [
                {
                    "name": "copy_table",
                    "type": "transfer",
                    "from_db": "gp",
                    "to_db": "trino",
                    "from_sql": "select * from source_table",
                    "to_table": "sandbox.copy",
                }
            ],
            concurrency=1,
            fail_fast=True,
            progress=False,
        )

    assert exc_info.value is error
    assert error.analytics_toolkit_sql_task_name == "copy_table"  # type: ignore[attr-defined]
    assert error.analytics_toolkit_sql_field == "from_sql"  # type: ignore[attr-defined]
    assert error.analytics_toolkit_sql_query == "select * from source_table"  # type: ignore[attr-defined]
    output = capsys.readouterr().out
    assert "SQL task 'copy_table' (transfer) failed while running from_sql" in output
    assert "from_sql:\nselect * from source_table" in output


def test_async_sql_lower_soft_cap_avoids_hard_cap_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    active_workers = 0
    max_active_workers = 0

    def fake_read_sql(**kwargs: Any) -> str:
        nonlocal active_workers, max_active_workers
        with lock:
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
        time.sleep(0.1)
        with lock:
            active_workers -= 1
        return kwargs["query"]

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": f"select {index}",
        }
        for index in range(11)
    ]

    result = async_module.async_sql(
        tasks,
        concurrency=11,
        soft_concurrency_cap=5,
        hard_concurrency_cap=10,
    )

    assert list(result) == [f"task_{index}" for index in range(11)]
    assert max_active_workers == 5


def test_async_sql_task_context_task_id_is_included_for_failed_tasks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_read_sql(
        *,
        db_key: str,
        query: str,
        print_queries: bool = False,
    ) -> pd.DataFrame:
        time_print("task output before failure")
        raise RuntimeError("boom")

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    with pytest.raises(RuntimeError, match="boom"):
        async_module.async_sql(
            [
                {
                    "name": "copy_table",
                    "type": "read",
                    "db_key": "gp",
                    "query": "select * from source limit 1",
                }
            ],
            concurrency=1,
        )

    assert "[task_id=copy_table]" in capsys.readouterr().out

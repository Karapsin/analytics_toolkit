from __future__ import annotations

from analytics_toolkit import sql as sql_module

from tests._support.concurrency import ConcurrencyProbe
from tests.sql._support.async_sql import (
    Any,
    pd,
    pytest,
    time_print,
)
from tests.sql._support.tasks import observe_task_cap, patch_task_operations


def test_async_sql_fail_fast_false_prints_failed_task_query(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        raise error

    patch_task_operations(monkeypatch, mode="async", read=fake_read_sql)

    result = sql_module.async_sql(
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

    patch_task_operations(monkeypatch, mode="async", transfer=fake_transfer_table)

    with pytest.raises(RuntimeError) as exc_info:
        sql_module.async_sql(
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
    probe = ConcurrencyProbe(5, held=True)

    def fake_read_sql(**kwargs: Any) -> str:
        with probe.worker():
            return kwargs["query"]

    patch_task_operations(monkeypatch, mode="async", read=fake_read_sql)

    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": f"select {index}",
        }
        for index in range(11)
    ]

    observation = observe_task_cap(monkeypatch, probe, mode="async", calls=11)
    result = observation.run(
        lambda: sql_module.async_sql(
            tasks,
            concurrency=11,
            soft_concurrency_cap=5,
            hard_concurrency_cap=10,
        )
    )

    assert list(result) == [f"task_{index}" for index in range(11)]
    probe.assert_complete(calls=11)


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

    patch_task_operations(monkeypatch, mode="async", read=fake_read_sql)

    with pytest.raises(RuntimeError, match="boom"):
        sql_module.async_sql(
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

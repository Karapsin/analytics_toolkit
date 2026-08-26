from __future__ import annotations

from tests.sql._support.async_sql import (
    Any,
    async_module,
    asyncio,
    pd,
    pytest,
)


def test_async_impl_resets_context_when_progress_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_create_task(coroutine: Any) -> None:
        coroutine.close()
        message = "task creation"
        raise RuntimeError(message)

    monkeypatch.setattr(
        async_module.asyncio,
        "create_task",
        fail_create_task,
    )
    with pytest.raises(RuntimeError, match="task creation"):
        asyncio.run(
            async_module._async_sql_impl(
                [{"type": "read", "db_key": "gp", "query": "select 1"}],
            )
        )
    assert async_module._CONCURRENCY_STATE.get() is None


def test_async_sql_suppresses_builtin_inner_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, dict[str, Any]] = {}
    df = pd.DataFrame({"id": [1]})

    def fake_execute_sql(**kwargs: Any) -> None:
        calls["execute"] = kwargs

    def fake_execute_read(**kwargs: Any) -> pd.DataFrame:
        calls["execute_read"] = kwargs
        return pd.DataFrame({"id": [1]})

    def fake_load_df(**kwargs: Any) -> int:
        calls["load_df"] = kwargs
        return 1

    def fake_transfer_table(**kwargs: Any) -> int:
        calls["transfer"] = kwargs
        return 2

    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(async_module, "execute_read", fake_execute_read)
    monkeypatch.setattr(async_module, "load_df", fake_load_df)
    monkeypatch.setattr(async_module, "transfer_table", fake_transfer_table)

    execute_spec = {
        "name": "refresh",
        "type": "execute",
        "db_key": "ch",
        "query": "select 1; select 2",
        "progress": True,
    }
    execute_read_spec = {
        "name": "prepare",
        "type": "execute_read",
        "db_key": "ch",
        "query": "select 1; select 2",
        "progress": True,
    }
    load_spec = {
        "name": "load_batch",
        "type": "load_df",
        "db_key": "gp",
        "destination_table": "sandbox.batch",
        "df": df,
        "progress": True,
    }
    transfer_spec = {
        "name": "copy_table",
        "type": "transfer",
        "from_db": "gp",
        "to_db": "trino",
        "from_sql": "select * from source",
        "to_table": "sandbox.copy",
        "progress": True,
    }

    result = async_module.async_sql(
        [execute_spec, execute_read_spec, load_spec, transfer_spec],
        concurrency=1,
        progress=False,
    )

    assert result["refresh"] == "success"
    pd.testing.assert_frame_equal(result["prepare"], pd.DataFrame({"id": [1]}))
    assert result["load_batch"] == 1
    assert result["copy_table"] == 2
    assert calls["execute"]["progress"] is False
    assert calls["execute_read"]["progress"] is False
    assert calls["load_df"]["progress"] is False
    assert calls["transfer"]["progress"] is False
    assert execute_spec["progress"] is True
    assert execute_read_spec["progress"] is True
    assert load_spec["progress"] is True
    assert transfer_spec["progress"] is True


def test_async_sql_updates_progress_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_execute_sql(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(async_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)

    result = async_module.async_sql(
        [
            {
                "type": "execute",
                "db_key": "gp",
                "query": "insert into target select 1",
            },
            {
                "type": "execute",
                "db_key": "gp",
                "query": "insert into target select 2",
            },
        ],
        concurrency=1,
        progress=True,
    )

    assert result == {
        "task_0": "success",
        "task_1": "success",
    }
    assert len(progress_bars) == 1
    progress_bar = progress_bars[0]
    assert progress_bar.kwargs == {
        "total": 2,
        "desc": "async_sql tasks",
        "unit": "task",
        "disable": False,
    }
    assert progress_bar.updates == [1, 1]
    assert progress_bar.closed

from __future__ import annotations

from tests.sql._support.parallel_sql import (
    Any,
    parallel_module,
    pytest,
)


def test_parallel_sql_updates_progress_bar(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(parallel_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(parallel_module, "execute_sql", fake_execute_sql)

    result = parallel_module.parallel_sql(
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
        "desc": "parallel_sql tasks",
        "unit": "task",
        "disable": False,
    }
    assert progress_bar.updates == [1, 1]
    assert progress_bar.closed

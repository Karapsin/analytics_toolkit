from __future__ import annotations

from tests.sql._support.load_table import (
    TEST_CH_TABLE,
    FakeClickHouseClient,
    load_df_module,
    pd,
)


def test_load_df_dry_run_does_not_create_progress_bar(monkeypatch) -> None:
    progress_bars: list[object] = []

    class FakeTqdm:
        def __init__(self, **kwargs: object) -> None:
            progress_bars.append(self)

        def update(self, value: int) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(load_df_module, "tqdm", FakeTqdm)

    plan = load_df_module.load_df(
        "gp",
        "sandbox.target",
        pd.DataFrame({"id": [1]}),
        dry_run=True,
    )

    assert plan.operation == "load_df"
    assert progress_bars == []


def test_load_df_progress_false_disables_bar(monkeypatch) -> None:
    client = FakeClickHouseClient()
    progress_bars: list[object] = []
    batch = pd.DataFrame({"id": [1, 2]})

    class FakeTqdm:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(load_df_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: client)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", lambda *args, **kwargs: 2)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    inserted_rows = load_df_module.load_df(
        "ch",
        TEST_CH_TABLE,
        batch,
        retry_cnt=1,
        timeout_increment=0,
        progress=False,
    )

    assert inserted_rows == 2
    assert len(progress_bars) == 1
    assert progress_bars[0].kwargs["disable"] is True
    assert progress_bars[0].updates == [2]
    assert progress_bars[0].closed is True


def test_load_df_updates_progress_bar(monkeypatch) -> None:
    client = FakeClickHouseClient()
    progress_bars: list[object] = []
    batch = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})

    class FakeTqdm:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_insert_table_batch(*args, **kwargs) -> int:
        df = args[3]
        kwargs["on_progress"](len(df))
        return len(df)

    monkeypatch.setattr(load_df_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(load_df_module, "get_sql_connection", lambda key: client)
    monkeypatch.setattr(load_df_module, "table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        load_df_module,
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(load_df_module, "insert_table_batch", fake_insert_table_batch)
    monkeypatch.setattr(load_df_module, "analyze_table", lambda *args, **kwargs: None)

    inserted_rows = load_df_module.load_df(
        "ch",
        TEST_CH_TABLE,
        batch,
        retry_cnt=1,
        timeout_increment=0,
        progress=True,
    )

    assert inserted_rows == 3
    assert len(progress_bars) == 1
    progress_bar = progress_bars[0]
    assert progress_bar.kwargs == {
        "total": 3,
        "desc": f"load_df ch.{TEST_CH_TABLE}",
        "unit": "row",
        "disable": False,
    }
    assert progress_bar.updates == [3]
    assert progress_bar.closed is True

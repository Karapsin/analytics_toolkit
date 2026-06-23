from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

source_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.io.source"
)


class FakeClickHouseStream:
    def __init__(self, blocks: list[pd.DataFrame]) -> None:
        self.blocks = blocks
        self.exit_calls = 0

    def __enter__(self) -> Any:
        return iter(self.blocks)

    def __exit__(self, *args: Any) -> None:
        self.exit_calls += 1


class FakeClickHouseConnection:
    def __init__(self, blocks: list[pd.DataFrame]) -> None:
        self.context = FakeClickHouseStream(blocks)
        self.queries: list[str] = []
        self.query_limit: int | None = None
        self.query_limits_seen: list[int | None] = []

    def query_df_stream(self, query: str) -> FakeClickHouseStream:
        self.queries.append(query)
        self.query_limits_seen.append(self.query_limit)
        return self.context


def test_clickhouse_batches_drain_pending_rows_without_spinning() -> None:
    connection = FakeClickHouseConnection(
        [
            pd.DataFrame({"id": [1, 2]}),
            pd.DataFrame({"id": [3, 4]}),
            pd.DataFrame({"id": [5]}),
        ]
    )
    batch_size_calls = 0

    def get_batch_size() -> int:
        nonlocal batch_size_calls
        batch_size_calls += 1
        if batch_size_calls > 10:
            raise AssertionError("ClickHouse batch draining is not making progress.")
        return 3

    batches = list(
        source_module.iter_source_batches(
            "ch",
            "ch",
            {"connection": connection},
            "select id from source",
            batch_size=3,
            retry_cnt=1,
            timeout_increment=0,
            get_batch_size=get_batch_size,
        )
    )

    assert [batch.rows for batch in batches] == [[(1,), (2,), (3,)], [(4,), (5,)]]
    assert connection.queries == ["select id from source"]
    assert connection.context.exit_calls == 1


def test_clickhouse_stream_temporarily_disables_client_query_limit() -> None:
    connection = FakeClickHouseConnection([pd.DataFrame({"id": [1, 2]})])
    connection.query_limit = 1_728_512

    batches = list(
        source_module.iter_source_batches(
            "ch",
            "ch",
            {"connection": connection},
            "select id from source limit 6582921",
            batch_size=10,
            retry_cnt=1,
            timeout_increment=0,
            disable_ch_query_limit=True,
        )
    )

    assert [batch.rows for batch in batches] == [[(1,), (2,)]]
    assert connection.query_limits_seen == [0]
    assert connection.query_limit == 1_728_512

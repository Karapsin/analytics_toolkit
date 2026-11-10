"""Lost acknowledgements replay data writes while structural uncertainty is protected."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql._support.outage import Client


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("operation", ["execute", "load"])
def test_clickhouse_lost_ack_data_writes_have_explicit_at_least_once_semantics(
    outage: Any, monkeypatch: pytest.MonkeyPatch, driver: str, operation: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    original = Client._insert
    submissions = 0

    def insert(client: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal submissions
        submissions += 1
        original(client, *args, **kwargs)
        if submissions == 1:
            message = "lost insert acknowledgement after accepted rows"
            raise ConnectionError(message)

    monkeypatch.setattr(Client, "_insert", insert)
    if operation == "execute":
        result = sql.execute(
            "ch",
            "INSERT INTO db.events VALUES (9)",
            retry_cnt=2,
            timeout_increment=0,
            return_metadata=True,
        )
        assert result.metadata.data_write_replay_possible is True
    else:
        sql.load_df(
            "ch",
            "db.events",
            pd.DataFrame({"id": [9]}),
            append=True,
            retry_cnt=2,
            timeout_increment=0,
        )
    assert submissions == 2
    assert (
        sum(
            obj.rows.count((9,))
            for (host, name), obj in cluster.tables.items()
            if name == "db.events_shard"
        )
        == 2
    )

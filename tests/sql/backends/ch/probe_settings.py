"""Internal cluster probes respect profile restrictions on both transports."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest
from analytics_toolkit import sql

from tests.sql._support.outage import Client, ProgrammingError


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("state", ["writable", "readonly", "missing"])
@pytest.mark.parametrize(
    "name",
    [
        "max_execution_time",
        "send_timeout",
        "receive_timeout",
        "connect_timeout_with_failover_ms",
        "connections_with_failover_max_tries",
    ],
)
def test_public_read_negotiates_optional_probe_timeouts(
    outage: Any, driver: str, state: str, name: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    if state == "missing":
        del cluster.probe_settings[name]
    elif state == "readonly":
        cluster.probe_settings[name] = ("60", 1)
    assert sql.read("ch", "SELECT id FROM db.events", retry_cnt=1).id.tolist() == [1, 2]
    assert cluster.probe_requests
    assert all((name in settings) == (state == "writable") for settings in cluster.probe_requests)
    assert all(settings["skip_unavailable_shards"] == 1 for settings in cluster.probe_requests)
    if name == "max_execution_time" and state == "writable":
        assert all(settings[name] == 5 for settings in cluster.probe_requests)


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("skip_unavailable_shards_mode", "unavailable_or_table_missing"),
    ],
)
def test_public_read_accepts_matching_immutable_probe_safety(
    outage: Any, driver: str, name: str, value: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.probe_settings[name] = (value, 1)
    assert sql.read("ch", "SELECT id FROM db.events", retry_cnt=1).id.tolist() == [1, 2]
    assert cluster.probe_requests
    assert all(name not in settings for settings in cluster.probe_requests)


@pytest.mark.parametrize("driver", ["http", "native"])
def test_complete_read_rejects_immutable_execution_conflict(outage: Any, driver: str) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.probe_settings["skip_unavailable_shards"] = ("1", 1)
    with pytest.raises(sql.ClickHouseClusterTopologyError, match="Operation requires setting"):
        sql.read("ch", "SELECT id FROM db.events", retry_cnt=1)
    assert cluster.probe_requests
    assert not cluster.selected_reads


@pytest.mark.parametrize("driver", ["http", "native"])
def test_partial_read_inherits_matching_immutable_execution_setting(
    outage: Any, driver: str
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    cluster.probe_settings["skip_unavailable_shards"] = ("1", 1)
    cluster.alive.remove("h2")
    with pytest.warns(sql.ClickHouseDegradedClusterWarning):
        result = sql.read(
            "ch", "SELECT id FROM db.events", retry_cnt=1, ch_unavailable_shards="healthy_shards"
        )
    assert result.id.tolist() == [1]


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize(
    ("name", "definition"),
    [
        ("skip_unavailable_shards", ("0", 1)),
        ("skip_unavailable_shards", None),
        ("skip_unavailable_shards_mode", ("unavailable_or_exception_before_processing", 1)),
    ],
)
def test_public_read_rejects_unavailable_required_probe_safety(
    outage: Any, driver: str, name: str, definition: Any
) -> None:
    cluster, configure = outage
    configure(driver=driver)
    if definition is None:
        del cluster.probe_settings[name]
    else:
        cluster.probe_settings[name] = definition
    with pytest.raises(sql.ClickHouseClusterTopologyError, match=name):
        sql.read("ch", "SELECT id FROM db.events", retry_cnt=1)
    assert not cluster.probe_requests
    assert not cluster.selected_reads


@pytest.mark.parametrize("driver", ["http", "native"])
def test_probe_settings_does_not_suppress_unrelated_driver_errors(
    outage: Any, monkeypatch: Any, driver: str
) -> None:
    _, configure = outage
    configure(driver=driver)
    original = Client.query

    def query(self: Any, statement: str, **kwargs: Any) -> Any:
        if "FROM system.settings" in statement:
            msg = "metadata access denied"
            raise ProgrammingError(msg)
        return original(self, statement, **kwargs)

    monkeypatch.setattr(Client, "query", query)
    with pytest.raises(ProgrammingError, match="metadata access denied"):
        sql.read("ch", "SELECT id FROM db.events", retry_cnt=1)


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("matching", [True, False])
@pytest.mark.parametrize(
    "case",
    [
        ("distributed_foreground_insert", "1", False),
        ("async_insert", "0", False),
        ("insert_shard_id", "1", True),
        ("parallel_distributed_insert_select", "0", True),
        ("optimize_skip_unused_shards", "0", True),
        ("optimize_distributed_group_by_sharding_key", "0", True),
    ],
)
def test_immutable_write_safety_settings(
    outage: Any, driver: str, matching: bool, case: tuple[str, str, bool]
) -> None:
    name, desired, partial = case
    cluster, configure = outage
    configure(driver=driver, ch_unavailable_shards="healthy_shards" if partial else "stop")
    if partial:
        cluster.alive.remove("h2")
    cluster.probe_settings[name] = (desired if matching else str(1 - int(desired)), 1)
    client = sql.get_sql_connection("ch")
    warning = pytest.warns(sql.ClickHouseDegradedClusterWarning) if partial else nullcontext()
    with warning:
        if matching:
            client.insert("db.events", [(9,)], column_names=["id"])
        else:
            with pytest.raises(sql.ClickHouseClusterTopologyError, match=name):
                client.insert("db.events", [(9,)], column_names=["id"])
    inserted = any((9,) in table.rows for table in cluster.tables.values())
    assert inserted == matching


@pytest.mark.parametrize("driver", ["http", "native"])
@pytest.mark.parametrize("matching", [True, False])
@pytest.mark.parametrize(
    "case",
    [
        ("distributed_ddl_task_timeout", "300", "0"),
        ("distributed_ddl_output_mode", "throw", "none"),
    ],
)
def test_immutable_ddl_safety_settings(
    outage: Any, driver: str, matching: bool, case: tuple[str, str, str]
) -> None:
    name, desired, conflict = case
    cluster, configure = outage
    configure(driver=driver)
    cluster.probe_settings[name] = (desired if matching else conflict, 1)
    client = sql.get_sql_connection("ch")
    if matching:
        client.command("TRUNCATE TABLE db.events_shard ON CLUSTER core")
    else:
        with pytest.raises(sql.ClickHouseClusterTopologyError, match=name):
            client.command("TRUNCATE TABLE db.events_shard ON CLUSTER core")
    rows = [row for table in cluster.tables.values() for row in table.rows]
    assert bool(rows) != matching


def test_fake_settings_metadata_honors_requested_names(outage: Any) -> None:
    cluster, _ = outage
    client = Client(cluster, "h1")
    result = client.query(
        "SELECT name, value, readonly FROM system.settings WHERE name IN ('async_insert')"
    )
    assert result.result_rows == [("async_insert", "0", 0)]


@pytest.mark.parametrize("driver", ["http", "native"])
def test_missing_required_write_setting_is_explicit(outage: Any, driver: str) -> None:
    cluster, configure = outage
    configure(driver=driver)
    del cluster.probe_settings["async_insert"]
    client = sql.get_sql_connection("ch")
    with pytest.raises(
        sql.ClickHouseClusterTopologyError, match="unavailable setting 'async_insert'"
    ):
        client.insert("db.events", [(9,)], column_names=["id"])

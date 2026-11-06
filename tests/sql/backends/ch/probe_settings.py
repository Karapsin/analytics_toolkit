"""Internal cluster probes respect profile restrictions on both transports."""

from __future__ import annotations

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

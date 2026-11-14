"""ClickHouse query replay validates the actual selected native encoder."""

from __future__ import annotations

import builtins
import json
from copy import deepcopy
from pathlib import Path

import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.core.exact_values import decode_value
from clickhouse_driver.errors import UnknownTypeError

from tests.sql._support.ch_native_aliases import http_factory_available
from tests.sql._support.query_client_validation import (
    alias_values,
    create,
    install_query_client,
    observe_client_factories,
)
from tests.sql.backends.ch._support.network_transport import encode_normalized


def public_query(*, source="analytics.events", drop=True, retry_cnt=1, **kwargs):
    return sql.create_table(
        "ch",
        "analytics.events",
        sql=f"SELECT payload FROM {source}",
        insert_data=True,
        drop_if_exists=drop,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        order_by="tuple()",
        return_metadata=True,
        retry_cnt=retry_cnt,
        timeout_increment=0,
        **kwargs,
    )


@pytest.mark.parametrize("native_type", ["Object('json')", "AggregateFunction(uniq, UInt64)"])
def test_query_server_sql_copy_never_constructs_client_codecs(
    monkeypatch, write_sql_connections, native_type
):
    values = [{"a": 1}] if native_type == "Object('json')" else [b"\x00"]
    client = install_query_client(
        monkeypatch, write_sql_connections, "http", native_type, values, exists=False
    )
    observe_client_factories(monkeypatch, client, forbid=True)

    result = public_query(source="analytics.source", drop=False)

    assert result.rows is None
    assert client.events == ["create", "server_copy"]
    assert client.scans == client.bound_rows == client.wire_columns == []
    assert client.rows == [{"payload": value} for value in values]
    assert any(f"`payload` {native_type}" in statement for statement in client.commands)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize(
    "native_type",
    [
        "SimpleAggregateFunction(sum, UInt64)",
        "Point",
        "Nested(x UInt64, y String)",
        "Object('json')",
    ],
    ids=["simple-aggregate", "point", "nested", "object"],
)
def test_frozen_query_preserves_alias_values_and_uses_selected_real_encoder(
    monkeypatch, write_sql_connections, transport, native_type
):
    values = alias_values(native_type, transport)
    client = install_query_client(
        monkeypatch, write_sql_connections, transport, native_type, values
    )
    observe_client_factories(monkeypatch, client)
    original = deepcopy(client.seed)

    if transport == "http" and not http_factory_available(native_type):
        with pytest.raises(ValueError, match=r"ClickHouse|encoder|[Uu]nsupported"):
            public_query()
        assert client.rows == original
        assert not client.commands
        assert not client.bound_rows
        assert "client_factory" in client.events
        if native_type == "Object('json')":
            assert client.values == values
        return

    if native_type == "Object('json')":
        terminal_values = [json.dumps(value, allow_nan=False) for value in values]
    elif transport == "http" and native_type == "Nested(x UInt64, y String)":
        terminal_values = [[{"x": 7, "y": b"a"}, {"x": 2**64 - 1, "y": b"b"}], []]
    else:
        terminal_values = deepcopy(values)
    expected_wire = encode_normalized(
        native_type, terminal_values, transport, settings=client.settings
    )
    result = public_query()

    assert result.metadata.input_count == len(values)
    record = result.metadata.type_resolutions[0]
    assert record["destination_type"] == native_type
    assert record["client_validation_driver"] == transport
    assert record["client_validation_certified_count"] == len(values)
    assert len(client.scans) == 1
    assert client.bound_rows == [(value,) for value in terminal_values]
    stored_values = values if native_type == "Object('json')" else terminal_values
    assert client.rows == [{"payload": value} for value in stored_values]
    assert client.wire_columns == [(native_type, expected_wire)]
    assert client.events.index("input_complete") < client.events.index("drop")
    assert client.events.index("client_factory") < client.events.index("drop")
    assert any(f"`payload` {native_type}" in statement for statement in client.commands)
    assert client.values == values


def test_frozen_object_query_canonicalizes_dict_and_retains_raw_json_text(
    monkeypatch, write_sql_connections
):
    raw_json = ' { "label" : "raw text", "n" : 2 } '
    values = [{"label": "é", "n": 1}, raw_json]
    original = deepcopy(values)
    client = install_query_client(
        monkeypatch, write_sql_connections, "native", "Object('json')", values
    )
    observe_client_factories(monkeypatch, client)
    terminal_values = ['{"label": "\\u00e9", "n": 1}', raw_json]
    expected_wire = encode_normalized("Object('json')", terminal_values, "native")

    result = public_query()

    assert result.metadata.input_count == 2
    assert client.bound_rows == [(value,) for value in terminal_values]
    assert client.rows == [
        {"payload": {"label": "é", "n": 1}},
        {"payload": {"label": "raw text", "n": 2}},
    ]
    assert client.wire_columns == [("Object('json')", expected_wire)]
    assert client.values == values == original
    assert len(client.scans) == 1


def test_frozen_query_invalid_last_object_is_rejected_before_live_ddl(
    monkeypatch, write_sql_connections, caplog
):
    private = "private late JSON failure"
    client = install_query_client(
        monkeypatch,
        write_sql_connections,
        "native",
        "Object('json')",
        [{"ok": 1}, '{"private":"' + private + '"'],
    )
    original = deepcopy(client.rows)
    observe_client_factories(monkeypatch, client)

    with pytest.raises(ValueError, match=r"ClickHouse|Object|encoder") as error:
        create.create_table_from_sql(
            "ch",
            "analytics.events",
            "SELECT payload FROM analytics.events",
            table_schema={"payload": "Object('json')"},
            drop_target_if_exists=True,
            ch_distributed_table=False,
            ch_engine="MergeTree",
            order_by="tuple()",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert client.rows == original
    assert client.commands == client.bound_rows == client.wire_columns == []
    assert len(client.scans) == 1
    assert private not in str(error.value)
    assert private not in caplog.text


def test_frozen_query_infers_exact_text_when_observed_object_values_do_not_fit_native_json(
    monkeypatch, write_sql_connections
):
    values = [{"ok": 1}, '{"private":"retained raw malformed text"']
    client = install_query_client(
        monkeypatch, write_sql_connections, "native", "Object('json')", values
    )
    observe_client_factories(monkeypatch, client)

    result = public_query()

    assert client.physical_type == "String"
    first = json.loads(client.rows[0]["payload"])
    assert first["backend"] == "ch"
    assert first["type"] == "Object('json')"
    assert decode_value(first["value"]) == values[0]
    assert client.rows[1]["payload"] == values[1]
    assert client.values == values
    assert len(client.scans) == 1
    assert client.events.index("input_complete") < client.events.index("drop")
    record = result.metadata.type_resolutions[0]
    assert record["destination_type"] == "String"
    assert record["encoding_version"] == 1


def test_frozen_empty_query_preserves_empty_creation_without_client_codecs(
    monkeypatch, write_sql_connections
):
    client = install_query_client(monkeypatch, write_sql_connections, "http", "Object('json')", [])
    observe_client_factories(monkeypatch, client, forbid=True)
    import_module = builtins.__import__

    def no_sdk_import(name, *args, **kwargs):
        assert not name.startswith(("clickhouse_driver", "clickhouse_connect"))
        return import_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk_import)
    result = public_query()

    assert result.metadata.input_count == 0
    record = result.metadata.type_resolutions[0]
    assert record["destination_type"] == "Object('json')"
    assert record["client_validation_basis"] == "certified-empty-input"
    assert record["client_validation_driver"] is None
    assert record["client_validation_certified_count"] == 0
    assert len(client.scans) == 1
    assert client.events == ["scan", "input_complete", "drop", "create"]
    assert client.rows == client.bound_rows == client.wire_columns == []
    assert any("`payload` Object('json')" in statement for statement in client.commands)


def test_frozen_query_unsupported_selected_native_codec_is_rejected_before_live_ddl(
    monkeypatch, write_sql_connections
):
    native_type = "AggregateFunction(uniq, UInt64)"
    client = install_query_client(
        monkeypatch, write_sql_connections, "native", native_type, [b"\x00", b"\x00"]
    )
    original = deepcopy(client.rows)
    observe_client_factories(monkeypatch, client)
    with pytest.raises(UnknownTypeError):
        encode_normalized(native_type, [b"\x00"], "native")

    with pytest.raises(ValueError, match=r"ClickHouse|authoritative|[Uu]nsupported|native"):
        create.create_table_from_sql(
            "ch",
            "analytics.events",
            "SELECT payload FROM analytics.events",
            table_schema={"payload": native_type},
            drop_target_if_exists=True,
            ch_distributed_table=False,
            ch_engine="MergeTree",
            order_by="tuple()",
            retry_cnt=1,
            timeout_increment=0,
        )

    assert client.rows == original
    assert client.commands == client.bound_rows == client.wire_columns == []


def test_frozen_native_query_resolves_bigint_to_the_actual_server_column_before_ddl(
    monkeypatch, write_sql_connections
):
    values = [-(2**63), 2**63 - 1]
    client = install_query_client(monkeypatch, write_sql_connections, "native", "Int64", values)
    observe_client_factories(monkeypatch, client)

    result = create.create_table_from_sql(
        "ch",
        "analytics.events",
        "SELECT payload FROM analytics.events",
        table_schema={"payload": "BIGINT"},
        drop_target_if_exists=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        order_by="tuple()",
        retry_cnt=1,
        return_metadata=True,
    )

    assert len(client.scans) == 1
    assert client.destination_type_probes
    assert all(probe == ["BIGINT"] for probe in client.destination_type_probes)
    assert client.events.index("destination_type_probe") < client.events.index("drop")
    assert client.physical_type == "Int64"
    assert client.normalization_hints == [("BIGINT",)]
    assert client.bound_rows == [(value,) for value in values]
    assert client.rows == [{"payload": value} for value in values]
    assert client.wire_columns == [("Int64", encode_normalized("Int64", values, "native"))]
    assert any("`payload` BIGINT" in statement for statement in client.commands)
    record = result.metadata.type_resolutions[0]
    assert record["destination_type"] == "BIGINT"
    assert record["client_validation_certified_count"] == 2


def test_frozen_query_lost_ack_replays_certified_rows_with_accepted_duplicate_effects(
    monkeypatch, write_sql_connections
):
    values = alias_values("Point", "native")
    client = install_query_client(monkeypatch, write_sql_connections, "native", "Point", values)
    observe_client_factories(monkeypatch, client)
    client.fail_insert_ack = 1

    result = public_query(retry_cnt=2)

    assert len(client.scans) == 1
    assert client.insert_attempts == 2
    assert client.bound_rows == [(value,) for value in values] * 2
    assert client.rows == [{"payload": value} for value in values] * 2
    assert client.wire_columns[0] == client.wire_columns[1]
    assert client.events.count("client_factory") == client.first_insert_factories
    assert client.first_insert_factories > 0
    assert result.metadata.input_count == len(values)
    assert result.metadata.data_write_replay_possible is True


def test_frozen_query_retry_rejects_changed_actual_client_configuration(
    monkeypatch, write_sql_connections
):
    values = alias_values("Point", "native")
    client = install_query_client(monkeypatch, write_sql_connections, "native", "Point", values)
    observe_client_factories(monkeypatch, client)
    client.fail_insert_ack = 1

    def change_selected_driver():
        configs = json.loads(Path(".connections").read_text())
        configs["ch"]["driver"] = "http"
        set_connections_path(write_sql_connections(configs))

    client.on_lost_ack = change_selected_driver

    retained_target = pytest.warns(
        RuntimeWarning,
        match=(
            r"Could not remove partial target 'analytics\.events' after a failed "
            r"create-table attempt: Connection configuration differs from certified transfer input"
        ),
    )
    scope_change = pytest.raises(ValueError, match="Connection configuration differs")
    with retained_target, scope_change:
        public_query(retry_cnt=2)

    assert len(client.scans) == 1
    assert client.insert_attempts == 1
    assert all(driver == "native" for _alias, driver, _host in client.opens)
    assert client.bound_rows == [(value,) for value in values]
    assert client.exists
    assert client.rows == [{"payload": value} for value in values]


@pytest.mark.parametrize("mode", ["dry_run", "return_sql"])
def test_query_client_capability_plans_do_not_import_sdk_or_scan_values(
    monkeypatch, write_sql_connections, mode
):
    client = install_query_client(
        monkeypatch, write_sql_connections, "http", "Object('json')", [{"private": 1}]
    )
    observe_client_factories(monkeypatch, client, forbid=True)
    import_module = builtins.__import__

    def no_sdk_import(name, *args, **kwargs):
        assert not name.startswith(("clickhouse_driver", "clickhouse_connect"))
        return import_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk_import)
    plan = create.create_table_from_sql(
        "ch",
        "analytics.events",
        "SELECT payload FROM analytics.events",
        table_schema={"payload": "Object('json')"},
        drop_target_if_exists=True,
        ch_distributed_table=False,
        ch_engine="MergeTree",
        order_by="tuple()",
        retry_cnt=1,
        **{mode: True},
    )

    assert plan.options["pending_input_validation"] is True
    assert plan.options["client_write_validation_pending"] is True
    assert plan.options["client_write_validation_condition"] == "python_value_replay"
    assert client.commands == client.scans == client.opens == client.bound_rows == []

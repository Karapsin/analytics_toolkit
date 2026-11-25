"""Client-certified dataframe values survive retry and offline planning boundaries."""

from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.general import set_connections_path
from analytics_toolkit.sql.execution.plans import SqlPlan

from tests.sql._support.load_client_validation import client_target
from tests.sql._support.load_table import load_df_module
from tests.sql.backends.ch._support.network_transport import encode_normalized


def configure_client(write_sql_connections, transport, *, host="clickhouse.example"):
    path = write_sql_connections(
        {
            "ch": {
                "type": "ch",
                "driver": transport,
                "host": host,
                "port": 9000 if transport == "native" else 8123,
                "user": "user",
                "password": "password",
                "database": "sandbox",
                "ddl_defaults": {
                    "regular": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "MergeTree", "on_cluster": None},
                    },
                    "staging": {
                        "create_distributed_pair": False,
                        "shard": {"engine": "MergeTree", "on_cluster": None},
                    },
                },
            }
        }
    )
    set_connections_path(path)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize("native_type", ["Ring", "Nested(x UInt64, y String)"])
def test_client_validation_retry_keeps_certified_structure_after_caller_mutation(
    monkeypatch, write_sql_connections, transport, native_type
):
    configure_client(write_sql_connections, transport)
    if native_type == "Ring":
        original = [[(1.25, 2.5), (3.5, 4.75), (1.25, 2.5)]]
        changed = (99.0, 100.0)
    else:
        original = [[(7, "old")]] if transport == "native" else [[{"x": 7, "y": "old"}]]
        changed = (99, "changed") if transport == "native" else {"x": 99, "y": "changed"}
    frame = pd.DataFrame({"payload": pd.Series(deepcopy(original), dtype=object)})
    store = client_target(monkeypatch, transport, {"payload": native_type})
    store.fail_generation = 1
    mutations = []

    def mutate_caller():
        frame.loc[0, "payload"].append(changed)
        mutations.append(True)

    store.on_lost_ack = mutate_caller
    result = sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="truncate_insert",
        retry_cnt=2,
        timeout_increment=0,
        return_metadata=True,
    )

    assert result.rows == 1
    assert store.generation == 2
    assert mutations == [True]
    assert frame.loc[0, "payload"] == [*original[0], changed]
    assert len(store.bound_rows) >= 2
    terminal_value = (
        [{"x": 7, "y": b"old"}]
        if native_type.startswith("Nested(") and transport == "http"
        else original[0]
    )
    assert all(row == (terminal_value,) for row in store.bound_rows)
    expected = encode_normalized(
        native_type, deepcopy(original), transport, settings=store.settings
    )
    assert all(column == ("payload", native_type, expected) for column in store.wire_columns)
    assert len(store.wire_columns) == len(store.bound_rows)
    assert store.rows == [{"payload": terminal_value}]
    assert result.metadata.source_rows == result.metadata.inserted_rows == 1
    assert len(result.metadata.type_resolutions) == 1
    assert all(
        resolution["client_validation_basis"] == "encoded-input"
        and resolution["client_certified_count"] == 1
        and resolution["client_validation_driver"] == transport
        for resolution in result.metadata.type_resolutions
    )


@pytest.mark.parametrize("transport", ["http", "native"])
def test_client_validation_retry_rejects_changed_alias_before_reusing_certified_values(
    monkeypatch, write_sql_connections, transport
):
    configure_client(write_sql_connections, transport)
    values = [[(1.25, 2.5), (3.5, 4.75)]]
    frame = pd.DataFrame({"payload": pd.Series(deepcopy(values), dtype=object)})
    original = frame.copy(deep=True)
    store = client_target(monkeypatch, transport, {"payload": "Ring"})
    store.on_lost_ack = lambda: configure_client(
        write_sql_connections, transport, host="retargeted.example"
    )

    with pytest.raises(ValueError, match="Destination configuration"):
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            write_mode="truncate_insert",
            retry_cnt=2,
            timeout_increment=0,
        )

    assert store.bound_rows
    assert all(row == (values[0],) for row in store.bound_rows)
    assert store.write_generations == [1] * len(store.bound_rows)
    assert [kind for kind, _detail in store.submissions] == [
        "CLEAR",
        *(["INSERT"] * len(store.bound_rows)),
    ]
    assert store.rows == [{"payload": values[0]}] * len(store.bound_rows)
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("transport", ["http", "native"])
@pytest.mark.parametrize(
    "case",
    [
        ("uint32", [1, 7], "UInt32"),
        ("UInt64", [0, None, 7], "Nullable(UInt64)"),
    ],
)
def test_client_validation_retry_preserves_inferred_unsigned_physical_schema(
    monkeypatch, write_sql_connections, transport, case
):
    dtype, values, native_type = case
    configure_client(write_sql_connections, transport)
    frame = pd.DataFrame({"payload": pd.Series(values, dtype=dtype)})
    original = frame.copy(deep=True)
    store = client_target(monkeypatch, transport, {}, exists=False)
    store.fail_generation = 1

    assert sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        write_mode="append",
        retry_cnt=2,
        timeout_increment=0,
        order_by="tuple()",
    ) == len(frame)

    assert store.generation == 2
    assert store.create_schemas
    assert all(schema == {"payload": native_type} for schema in store.create_schemas)
    assert store.schema == {"payload": native_type}
    expected = encode_normalized(native_type, values, transport)
    assert len(store.wire_columns) >= 2
    assert all(column == ("payload", native_type, expected) for column in store.wire_columns)
    copies, remainder = divmod(len(store.rows), len(values))
    assert remainder == 0
    assert 1 <= copies <= len(store.write_generations)
    assert store.rows == [{"payload": value} for value in values] * copies
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("planning_flag", ["dry_run", "return_sql"])
def test_client_validation_offline_plan_marks_nonempty_input_without_sdk_or_connection(
    monkeypatch, write_sql_connections, planning_flag
):
    configure_client(write_sql_connections, "native")
    calls = []

    def unexpected(*_args, **_kwargs):
        calls.append(True)
        message = "Offline dataframe planning attempted client or connection I/O."
        raise AssertionError(message)

    monkeypatch.setattr(load_df_module, "get_sql_connection", unexpected)
    monkeypatch.setattr("clickhouse_driver.columns.service.get_column_by_spec", unexpected)
    monkeypatch.setattr("clickhouse_connect.datatypes.registry.get_from_name", unexpected)
    values = [[(1.25, 2.5), (3.5, 4.75)]]
    frame = pd.DataFrame({"payload": pd.Series(deepcopy(values), dtype=object)})

    plan = sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        table_schema={"payload": "Ring"},
        write_mode="truncate_insert",
        order_by="tuple()",
        **{planning_flag: True},
    )

    assert isinstance(plan, SqlPlan)
    assert plan.options["pending_client_write_validation"] is True
    assert plan.metadata.source_rows == 1
    assert calls == []
    assert frame.loc[0, "payload"] == values[0]
    assert not any(statement.phase == "clear_target" for statement in plan.statements)
    assert plan.options["pending_existing_layout_verification"] is True
    assert plan.options["pending_resource_scope_resolution"] is True
    assert plan.options["execution_requires"]
    assert plan.metadata.statement_count is None


def test_client_validation_empty_sql_only_creation_needs_no_unsupported_http_encoder(
    monkeypatch, write_sql_connections
):
    configure_client(write_sql_connections, "http")
    store = client_target(monkeypatch, "http", {}, exists=False)
    calls = []

    def unexpected(*_args, **_kwargs):
        calls.append(True)
        message = "An empty SQL-only creation constructed a Python value encoder."
        raise AssertionError(message)

    monkeypatch.setattr("clickhouse_connect.datatypes.registry.get_from_name", unexpected)
    frame = pd.DataFrame({"payload": pd.Series([], dtype=object)})
    assert (
        sql.load_df(
            "ch",
            "sandbox.events",
            frame,
            table_schema={"payload": "Object('json')"},
            write_mode="append",
            empty_source_policy="replace",
            order_by="tuple()",
            retry_cnt=1,
            timeout_increment=0,
        )
        == 0
    )

    assert calls == []
    assert store.create_schemas == [{"payload": "Object('json')"}]
    assert store.rows == store.bound_rows == store.wire_columns == []
    assert store.schema == {"payload": "Object('json')"}


def test_client_validation_empty_offline_plan_does_not_claim_pending_python_values(
    monkeypatch, write_sql_connections
):
    configure_client(write_sql_connections, "http")

    def unexpected(*_args, **_kwargs):
        message = "An empty offline plan attempted a SQL connection."
        raise AssertionError(message)

    monkeypatch.setattr(load_df_module, "get_sql_connection", unexpected)
    plan = sql.load_df(
        "ch",
        "sandbox.events",
        pd.DataFrame({"payload": pd.Series([], dtype=object)}),
        table_schema={"payload": "Object('json')"},
        write_mode="append",
        empty_source_policy="replace",
        order_by="tuple()",
        dry_run=True,
    )

    assert plan.options["pending_client_write_validation"] is False
    assert plan.metadata.source_rows == 0
    assert any("Object('json')" in statement.sql for statement in plan.statements)


@pytest.mark.parametrize("transport", ["http", "native"])
def test_client_validation_new_bigint_declaration_keeps_raw_ddl_and_physical_int64(
    monkeypatch, write_sql_connections, transport
):
    configure_client(write_sql_connections, transport)
    store = client_target(monkeypatch, transport, {}, exists=False)
    store.fail_generation = 0
    values = [7, -3]
    frame = pd.DataFrame({"payload": values})
    original = frame.copy(deep=True)

    assert sql.load_df(
        "ch",
        "sandbox.events",
        frame,
        table_schema={"payload": "BIGINT"},
        write_mode="append",
        order_by="tuple()",
        retry_cnt=1,
        timeout_increment=0,
    ) == len(values)

    assert store.create_schemas == [{"payload": "Int64"}]
    assert "BIGINT" in store.ddls[("sandbox", "events")]
    assert len(store.type_probes) == 1
    probe, schema_at_probe, rows_at_probe = store.type_probes[0]
    assert "defaultValueOfTypeName('BIGINT')" in probe
    assert probe in store.metadata_queries
    assert schema_at_probe == {}
    assert rows_at_probe == []
    assert store.schema == {"payload": "Int64"}
    assert store.rows == [{"payload": value} for value in values]
    assert store.bound_rows == [(value,) for value in values]
    assert store.wire_columns == [
        ("payload", "Int64", encode_normalized("Int64", values, transport))
    ]
    pd.testing.assert_frame_equal(frame, original)

"""Disposable Greenplum JSON validation and native driver serialization checks."""

from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from psycopg2.extras import Json

from tests.sql.integration._support.backends import backend_enabled, integration_table
from tests.sql.integration._support.identity import query_label
from tests.sql.integration.manifest import scenario_param

if TYPE_CHECKING:
    from typing import Any

    from tests.sql.integration._support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _json_rows(alias: str, table: str) -> list[tuple[int, bool, Any]]:
    frame = sql.read(
        alias,
        f"SELECT row_id, payload IS NULL AS sql_null, payload::text AS json_text "
        f"FROM {table} ORDER BY row_id",
        retry_cnt=1,
        query_label=_label("gp-json-rows"),
    )
    rows = []
    for row_id, sql_null, encoded in frame.itertuples(index=False, name=None):
        missing = bool(sql_null)
        if missing:
            assert pd.isna(encoded)
            value = None
        else:
            assert isinstance(encoded, str)
            value = json.loads(encoded)
        rows.append((int(row_id), missing, value))
    return rows


def _valid_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4, 5, 6],
            "payload": pd.Series(
                [
                    {"kind": "dict", "number": 2**53 + 1, "items": [True, None, "я"]},
                    [2**53 + 1, False, None, {"text": "я"}],
                    None,
                    "null",
                    '{"raw":[1,false]}',
                    '"quoted string"',
                ],
                dtype=object,
            ),
        }
    )


def _check_matrix(alias: str, table: str, first: str, last: str) -> None:
    adapter = get_backend_adapter("gp")
    first_json = adapter.render_transfer_literal(json.dumps({"key": first}))
    last_json = adapter.render_transfer_literal(json.dumps({"key": last}))
    result = sql.read(
        alias,
        f"SELECT array_ndims(payload) AS dimensions, "
        f"payload[1][1] = {first_json}::jsonb AS first_equal, "
        "payload[1][2] IS NULL AS sql_null_element, "
        "payload[2][1] IS NOT NULL AND payload[2][1] = 'null'::jsonb AS json_null_element, "
        "payload[2][2] IS NOT NULL AND payload[2][2] = '\"null\"'::jsonb AS string_null_element, "
        f"payload[2][3] = {last_json}::jsonb AS last_equal FROM {table} WHERE row_id = 1",
        retry_cnt=1,
        query_label=_label("gp-json-matrix-check"),
    )
    assert len(result) == 1
    assert int(result.iloc[0]["dimensions"]) == 2
    for column in (
        "first_equal",
        "sql_null_element",
        "json_null_element",
        "string_null_element",
        "last_equal",
    ):
        assert not pd.isna(result.iloc[0][column])
        assert bool(result.iloc[0][column])
    assert sql.read(
        alias,
        f"SELECT payload IS NULL FROM {table} WHERE row_id = 2",
        output_type="scalar",
        retry_cnt=1,
        query_label=_label("gp-json-matrix-null"),
    )


def _assert_native_jsonb_matrix(alias: str, resource_registry: ResourceRegistry) -> None:
    table = resource_registry.table(alias, integration_table("gp", "json_matrix"))
    schema = {"row_id": "BIGINT", "payload": "JSONB[][]"}
    for mode, first, last in (("replace", "first", "last"), ("truncate_insert", "updated", "tail")):
        frame = pd.DataFrame(
            {
                "row_id": [1, 2],
                "payload": pd.Series(
                    [
                        [
                            [{"key": first}, None, {"extra": "cell"}],
                            [Json(None), "null", {"key": last}],
                        ],
                        None,
                    ],
                    dtype=object,
                ),
            }
        )
        assert (
            sql.load_df(
                alias,
                table,
                frame,
                table_schema=schema,
                write_mode=mode,
                gp_distributed_by_key="row_id",
                retry_cnt=1,
                query_label=_label("gp-json-matrix-load"),
            )
            == 2
        )
        _check_matrix(alias, table, first, last)
        # PostgreSQL's catalog type loses the declared SQL array dimension count.
        # Keep the explicit 2D schema on the second call to retain caller intent.
        table_literal = get_backend_adapter("gp").render_transfer_literal(table)
        assert (
            sql.read(
                alias,
                "SELECT format_type(atttypid, atttypmod) FROM pg_catalog.pg_attribute "
                f"WHERE attrelid = {table_literal}::regclass "
                "AND attname = 'payload' AND NOT attisdropped",
                output_type="scalar",
                retry_cnt=1,
                query_label=_label("gp-json-matrix-catalog"),
            )
            == "jsonb[]"
        )


@pytest.mark.parametrize(
    "native_type",
    [
        scenario_param(f"audit.load.gp_json.{native.lower()}", native)
        for native in ("JSON", "JSONB")
    ],
)
def test_greenplum_json_existing_and_explicit_loads_preserve_native_values(
    native_type: str,
    monkeypatch: pytest.MonkeyPatch,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    alias = "gp_target"
    existing = resource_registry.table(alias, integration_table("gp", "json_existing"))
    explicit = resource_registry.table(alias, integration_table("gp", "json_explicit"))
    sql.execute(
        alias,
        f"CREATE TABLE {existing} (row_id BIGINT NOT NULL, payload {native_type}) "
        "DISTRIBUTED BY (row_id)",
        retry_cnt=1,
        query_label=_label("gp-json-create"),
    )
    sql.execute(
        alias,
        f"INSERT INTO {existing} (row_id, payload) VALUES (99, '{{\"seed\":true}}')",
        retry_cnt=1,
        query_label=_label("gp-json-seed"),
    )
    seeded = [(99, False, {"seed": True})]
    assert _json_rows(alias, existing) == seeded
    adapter = get_backend_adapter("gp")
    execute_command, insert_dataframe_batch = (
        adapter.execute_command,
        adapter.insert_dataframe_batch,
    )
    commands: list[str] = []
    inserts: list[str] = []

    def record_command(connection: Any, statement: str) -> Any:
        commands.append(statement)
        return execute_command(connection, statement)

    def record_insert(connection: Any, table_name: str, batch: Any, **kwargs: Any) -> Any:
        inserts.append(table_name)
        return insert_dataframe_batch(connection, table_name, batch, **kwargs)

    with monkeypatch.context() as operations:
        operations.setattr(adapter, "execute_command", record_command)
        operations.setattr(adapter, "insert_dataframe_batch", record_insert)
        for invalid in ({"decimal": Decimal("1.25")}, "{invalid-json"):
            with pytest.raises((TypeError, ValueError)):
                sql.load_df(
                    alias,
                    existing,
                    pd.DataFrame({"row_id": [100], "payload": pd.Series([invalid], dtype=object)}),
                    write_mode="truncate_insert",
                    retry_cnt=1,
                    query_label=_label("gp-json-reject"),
                    gp_distributed_by_key="row_id",
                )
            assert commands == []
            assert inserts == []
            assert _json_rows(alias, existing) == seeded

        incoming = _valid_frame()
        assert sql.load_df(
            alias,
            existing,
            incoming,
            write_mode="truncate_insert",
            retry_cnt=1,
            query_label=_label("gp-json-existing-load"),
            gp_distributed_by_key="row_id",
        ) == len(incoming)
        assert sql.load_df(
            alias,
            explicit,
            incoming,
            table_schema={"row_id": "BIGINT", "payload": native_type},
            write_mode="replace",
            retry_cnt=1,
            query_label=_label("gp-json-explicit-load"),
            gp_distributed_by_key="row_id",
        ) == len(incoming)
    assert commands
    assert inserts
    expected = [
        (1, False, {"kind": "dict", "number": 2**53 + 1, "items": [True, None, "я"]}),
        (2, False, [2**53 + 1, False, None, {"text": "я"}]),
        (3, True, None),
        (4, False, None),
        (5, False, {"raw": [1, False]}),
        (6, False, "quoted string"),
    ]
    assert _json_rows(alias, existing) == expected
    assert _json_rows(alias, explicit) == expected
    if native_type == "JSONB":
        _assert_native_jsonb_matrix(alias, resource_registry)

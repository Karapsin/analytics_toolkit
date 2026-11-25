"""Native JSON documents retain their type through public transfer and binding."""

from __future__ import annotations

import json

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from sqlglot import exp, parse_one

from tests.sql._support.exact_trino_transfer import (
    RawTransferSource,
    attempt,
    install_raw_trino_transfer,
)
from tests.sql._support.exact_trino_values import raw_type
from tests.sql._support.trino_parameters import DriverParameterConnection


def test_native_json_public_transfer_preserves_documents_and_sql_null(
    monkeypatch, write_sql_connections
):
    documents = ["1", '{"number":1}', "null", '"text"', None]
    source = RawTransferSource(
        "json",
        raw_type("json"),
        {"SELECT stamp FROM source": [[[document] for document in documents]]},
    )
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        table_schema={"stamp": "JSON"},
    )
    connection = DriverParameterConnection()
    load = attempt.load_stage_batches

    def load_and_bind(options, connection_refs, stage_state, *args, **kwargs):
        count = load(options, connection_refs, stage_state, *args, **kwargs)
        get_backend_adapter("trino")._insert_rows(
            connection,
            "iceberg.sandbox.stage",
            ["stamp"],
            scenario.loaded[-1],
            target_column_types={"stamp": "JSON"},
        )
        return count

    monkeypatch.setattr(attempt, "load_stage_batches", load_and_bind)

    assert scenario.run() == len(documents)

    stored = []
    for statement, _params, wire in connection.statements:
        expressions = parse_one(statement, read="trino").expression.expressions
        for row, raw in zip(expressions, wire):
            parameter = parse_one(raw, read="trino")
            if isinstance(parameter, exp.Null):
                stored.append(None)
                continue
            expression = row.expressions[0]
            if isinstance(expression, exp.ParseJSON):
                stored.append(json.dumps(json.loads(parameter.this), separators=(",", ":")))
            else:
                assert isinstance(expression, exp.Cast)
                stored.append(json.dumps(parameter.this))
    assert stored == ["1", '{"number":1}', "null", '"text"', None]


@pytest.mark.parametrize("container", ["array", "map", "row"])
def test_native_json_public_transfer_binds_nested_documents_without_string_casts(
    monkeypatch, write_sql_connections, container
):
    json_type = raw_type("json")
    if container == "array":
        native = "ARRAY(JSON)"
        signature = raw_type("array", {"kind": "TYPE", "value": json_type})
        value = ["1", "null", None]
    elif container == "map":
        native = "MAP(VARCHAR, JSON)"
        signature = raw_type(
            "map",
            {"kind": "TYPE", "value": raw_type("varchar")},
            {"kind": "TYPE", "value": json_type},
        )
        value = {"number": "1", "json-null": "null", "sql-null": None}
    else:
        native = "ROW(number JSON, missing JSON)"
        signature = raw_type(
            "row",
            *[
                {
                    "kind": "NAMED_TYPE",
                    "value": {
                        "fieldName": {"name": name, "delimited": False},
                        "typeSignature": json_type,
                    },
                }
                for name in ("number", "missing")
            ],
        )
        value = ["1", "null"]
    source = RawTransferSource(
        native,
        signature,
        {"SELECT stamp FROM source": [[[None], [value]]]},
    )
    scenario = install_raw_trino_transfer(
        monkeypatch,
        write_sql_connections,
        source,
        table_schema={"stamp": native},
    )
    connection = DriverParameterConnection()
    load = attempt.load_stage_batches

    def load_and_bind(options, connection_refs, stage_state, *args, **kwargs):
        count = load(options, connection_refs, stage_state, *args, **kwargs)
        get_backend_adapter("trino")._insert_rows(
            connection,
            "iceberg.sandbox.stage",
            ["stamp"],
            scenario.loaded[-1],
            target_column_types={"stamp": native},
        )
        return count

    monkeypatch.setattr(attempt, "load_stage_batches", load_and_bind)

    assert scenario.run() == 2
    statement, parameters, wire = connection.statements[-1]
    tree = parse_one(statement, read="trino")
    assert len(list(tree.find_all(exp.ParseJSON))) == 2
    assert [item for item in parameters if item in {"1", "null"}] == ["1", "null"]
    assert wire[0] == "NULL"

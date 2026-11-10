"""Actual Arrow/Parquet fixtures for recursively resolved Hive stage types."""

from __future__ import annotations

import io
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import fsspec
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.trino.arrow_types import (
    arrow_type_for_trino_stage,
    hive_stage_type,
    parquet_scalar,
)
from analytics_toolkit.sql.core.exact_values import ExactTemporal, OrderedMap, OrderedMapPairs
from analytics_toolkit.sql.dml.transfer.flow.parquet_stage import (
    row_batch_to_arrow_table,
    write_batch_to_parquet_stage,
    write_dataframe_to_parquet_stage,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch
from sqlglot import exp, parse_one

TYPES = {
    "events": (
        'ARRAY(ROW("event id" UUID, "at" TIMESTAMP(9) WITH TIME ZONE, amount DECIMAL(29,9)))'
    ),
    "labels": 'MAP(UUID, ARRAY(ROW("raw bytes" VARBINARY, "quoted.field" ARRAY(UUID))))',
    "ordered": "MAP(VARCHAR, DECIMAL(12,4))",
    "positional": "ROW(UUID, ARRAY(TIMESTAMP(6) WITH TIME ZONE), MAP(VARCHAR, BIGINT))",
    "nested_amounts": "ARRAY(MAP(VARCHAR, ARRAY(DECIMAL(12,4))))",
}


def _batch(*, mapping_row: bool = False) -> RowBatch:
    event = (
        UUID(int=1),
        pd.Timestamp("2024-01-01T03:00:00.123456789+03:00"),
        Decimal("12345678901234567890.123456789"),
    )
    event_value = dict(zip(("event id", "at", "amount"), event)) if mapping_row else event
    return RowBatch(
        list(TYPES),
        [
            (None,) * len(TYPES),
            (
                [event_value],
                {UUID(int=2): [(memoryview(b"\x00\xff"), [UUID(int=3), None])]},
                OrderedMapPairs((("same", Decimal("1.2300")), ("same", Decimal("2.3400")))),
                (UUID(int=4), [pd.Timestamp("2024-01-01T00:00:00.000001Z")], {"n": 2**53 + 1}),
                [{"amount": [Decimal("0.0001"), None]}],
            ),
        ],
    )


def _types() -> list[pa.DataType]:
    return [
        pa.list_(
            pa.struct(
                [
                    pa.field("event id", pa.string()),
                    pa.field("at", pa.string()),
                    pa.field("amount", pa.decimal128(29, 9)),
                ]
            )
        ),
        pa.map_(
            pa.string(),
            pa.list_(
                pa.struct(
                    [
                        pa.field("raw bytes", pa.binary()),
                        pa.field("quoted.field", pa.list_(pa.string())),
                    ]
                )
            ),
        ),
        pa.map_(pa.string(), pa.decimal128(12, 4)),
        pa.struct(
            [
                pa.field("_field_0", pa.string()),
                pa.field("_field_1", pa.list_(pa.string())),
                pa.field("_field_2", pa.map_(pa.string(), pa.int64())),
            ]
        ),
        pa.list_(pa.map_(pa.string(), pa.list_(pa.decimal128(12, 4)))),
    ]


def _assert_exact(table: pa.Table) -> None:
    assert table.schema.types == _types()
    assert table.column("events").to_pylist() == [
        None,
        [
            {
                "event id": str(UUID(int=1)),
                "at": "2024-01-01T00:00:00.123456789+00:00",
                "amount": Decimal("12345678901234567890.123456789"),
            }
        ],
    ]
    assert table.column("labels").to_pylist() == [
        None,
        [
            (
                str(UUID(int=2)),
                [{"raw bytes": b"\x00\xff", "quoted.field": [str(UUID(int=3)), None]}],
            )
        ],
    ]
    assert table.column("ordered").to_pylist() == [
        None,
        [("same", Decimal("1.2300")), ("same", Decimal("2.3400"))],
    ]
    assert table.column("positional").to_pylist() == [
        None,
        {
            "_field_0": str(UUID(int=4)),
            "_field_1": ["2024-01-01T00:00:00.000001+00:00"],
            "_field_2": [("n", 2**53 + 1)],
        },
    ]
    assert table.column("nested_amounts").to_pylist() == [
        None,
        [[("amount", [Decimal("0.0001"), None])]],
    ]


@pytest.mark.parametrize("mapping_row", [False, True])
def test_nested_native_types_keep_exact_arrow_values(mapping_row: bool) -> None:
    table = row_batch_to_arrow_table(pa, _batch(mapping_row=mapping_row), column_types=TYPES)
    data = io.BytesIO()
    pq.write_table(table, data)
    _assert_exact(pq.read_table(pa.BufferReader(data.getvalue())))


@pytest.mark.parametrize("rows", [[], [(None,) * len(TYPES)]])
def test_all_null_and_empty_nested_columns_use_the_resolved_schema(rows: list) -> None:
    table = row_batch_to_arrow_table(pa, RowBatch(list(TYPES), rows), column_types=TYPES)
    data = io.BytesIO()
    pq.write_table(table, data)
    assert pq.read_table(pa.BufferReader(data.getvalue())).schema.types == _types()


@pytest.mark.parametrize("dataframe", [False, True])
def test_nested_dataframe_and_keyed_row_files_share_physical_types(dataframe: bool) -> None:
    location = "memory://nested-parquet/" + ("df" if dataframe else "keyed")
    batch = _batch()
    kwargs = {
        "stage_external_location": location,
        "pa": pa,
        "pq": pq,
        "fsspec_module": fsspec,
        "row_group_size": 1,
        "column_types": TYPES,
    }
    if dataframe:
        frame = pd.DataFrame(
            {
                name: pd.Series([row[index] for row in batch.rows], dtype=object)
                for index, name in enumerate(batch.columns)
            }
        )
        assert write_dataframe_to_parquet_stage(frame, **kwargs) == 2
        names = [f"part-{index:05d}.parquet" for index in range(2)]
    else:
        for index, row in enumerate(batch.rows):
            assert (
                write_batch_to_parquet_stage(
                    RowBatch(batch.columns, [row]), file_index=index, slice_index=7, **kwargs
                )
                == 1
            )
        names = [f"slice-00007-part-{index:05d}.parquet" for index in range(2)]
    tables = []
    for name in names:
        with fsspec.open(f"{location}/{name}", "rb") as stream:
            table = pq.read_table(pa.BufferReader(stream.read()))
        assert table.schema.types == _types()
        tables.append(table)
    _assert_exact(pa.concat_tables(tables))


def test_nested_hive_ddl_matches_physical_child_encodings_and_preserves_names() -> None:
    query = get_backend_adapter("trino").build_parquet_stage_table_sql(
        "hive.staging.nested", TYPES, "memory://nested/"
    )
    tree = parse_one(query, read="trino")
    columns = {item.name: item.args["kind"] for item in tree.this.expressions}
    assert columns["events"].sql(dialect="trino") == (
        'ARRAY(ROW("event id" VARCHAR, "at" VARCHAR, amount DECIMAL(29, 9)))'
    )
    assert columns["labels"].sql(dialect="trino") == (
        'MAP(VARCHAR, ARRAY(ROW("raw bytes" VARBINARY, "quoted.field" ARRAY(VARCHAR))))'
    )
    assert columns["positional"].sql(dialect="trino") == (
        'ROW("_field_0" VARCHAR, "_field_1" ARRAY(VARCHAR), "_field_2" MAP(VARCHAR, BIGINT))'
    )
    assert not any(
        item.this.value in {"UUID", "TIMESTAMPTZ"} for item in tree.find_all(exp.DataType)
    )


@pytest.mark.parametrize(
    ("native", "physical"),
    [
        ("CHAR(4)", pa.string()),
        ("VARCHAR(20)", pa.string()),
        ("TEXT", pa.string()),
        ("BINARY", pa.binary()),
        ("VARBINARY", pa.binary()),
        ("BOOLEAN", pa.bool_()),
        ("TINYINT", pa.int64()),
        ("SMALLINT", pa.int64()),
        ("INTEGER", pa.int64()),
        ("BIGINT", pa.int64()),
        ("REAL", pa.float64()),
        ("DOUBLE PRECISION", pa.float64()),
        ("DATE", pa.date32()),
        ("DECIMAL", pa.decimal128(38, 0)),
        ("DECIMAL(12)", pa.decimal128(12, 0)),
        ("DECIMAL(38,9)", pa.decimal128(38, 9)),
        ("TIMESTAMP(3)", pa.timestamp("us")),
        ("TIMESTAMP(9)", pa.string()),
        ("TIMESTAMP(9) WITH TIME ZONE", pa.string()),
    ],
)
def test_supported_scalar_aliases_have_schema_derived_arrow_types(
    native: str, physical: pa.DataType
) -> None:
    assert arrow_type_for_trino_stage(pa, native) == physical


def test_recursive_scalar_conversions_never_use_inference() -> None:
    assert parquet_scalar(np.array([1, None], dtype=object), "ARRAY(BIGINT)") == [1, None]
    assert parquet_scalar(OrderedMap((("x", bytearray(b"y")),)), "MAP(VARCHAR, VARBINARY)") == [
        ("x", b"y")
    ]
    assert parquet_scalar([Decimal("0.25")], "ARRAY(REAL)") == [0.25]
    assert parquet_scalar([Decimal("0.0001")], "ARRAY(VARCHAR)") == ["0.0001"]
    assert parquet_scalar([ExactTemporal(-1, 6)], "ARRAY(TIMESTAMP(6))") == [
        pd.Timestamp("1969-12-31T23:59:59.999999")
    ]
    assert parquet_scalar(UUID(int=1)) == str(UUID(int=1))
    assert parquet_scalar(memoryview(b"x")) == b"x"
    assert hive_stage_type("BINARY") == "VARBINARY"
    assert hive_stage_type("TEXT") == "VARCHAR"
    assert hive_stage_type("ROW(_field_1 UUID, BIGINT)") == (
        'ROW(_field_1 VARCHAR, "_field_1_" BIGINT)'
    )


@pytest.mark.parametrize("native", ["CUSTOM_TYPE", "ARRAY(CUSTOM_TYPE)", "MAP(VARCHAR, TIME)"])
def test_unsupported_resolved_type_is_rejected_before_ddl_or_upload(native: str) -> None:
    assert arrow_type_for_trino_stage(pa, native) is None
    with pytest.raises(ValueError, match="Unsupported resolved Trino Parquet stage type"):
        get_backend_adapter("trino").build_parquet_stage_table_sql(
            "hive.staging.unsupported", {"value": native}, "memory://unsupported/"
        )
    with pytest.raises(ValueError, match="Unsupported resolved Trino Parquet stage type"):
        write_batch_to_parquet_stage(
            RowBatch(["value"], [(None,)]),
            file_index=0,
            stage_external_location="memory://unsupported/",
            pa=pa,
            pq=SimpleNamespace(),
            fsspec_module=SimpleNamespace(),
            row_group_size=1,
            column_types={"value": native},
        )


@pytest.mark.parametrize(
    ("value", "native", "error", "message"),
    [
        (1, "ARRAY(BIGINT)", TypeError, "sequence"),
        (np.array(1), "ARRAY(BIGINT)", TypeError, "sequence"),
        (["ab"], "MAP(VARCHAR, VARCHAR)", TypeError, "sequence"),
        ([(None, 1)], "MAP(VARCHAR, BIGINT)", ValueError, "non-NULL"),
        ([(1, 2, 3)], "MAP(BIGINT, BIGINT)", ValueError, "key/value"),
        ({"a": 1}, "ROW(a BIGINT, b BIGINT)", ValueError, "mapping keys"),
        ((1,), "ROW(a BIGINT, b BIGINT)", ValueError, "width"),
        ((1, 2), "ROW(a BIGINT, a BIGINT)", ValueError, "distinct names"),
        ((), "ROW()", ValueError, "at least one field"),
    ],
)
def test_structural_mismatches_are_rejected_without_guessing_fields(
    value: object, native: str, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        parquet_scalar(value, native)


def test_partial_resolved_schema_cannot_infer_missing_columns() -> None:
    with pytest.raises(ValueError, match="Every Parquet stage column"):
        row_batch_to_arrow_table(pa, RowBatch(["x"], [(None,)]), column_types={})


@pytest.mark.parametrize("aware", [False, True])
def test_nested_high_precision_timestamps_use_exact_text_without_changing_destination(
    aware: bool,
) -> None:
    temporal_type = "TIMESTAMP(9)" + (" WITH TIME ZONE" if aware else "")
    target_type = f"ARRAY(ROW(moment {temporal_type}))"
    value = pd.Timestamp("2024-01-01T00:00:00.000001000" + ("Z" if aware else ""))
    table = row_batch_to_arrow_table(
        pa, RowBatch(["value"], [([(value,)],), (None,)]), column_types={"value": target_type}
    )
    assert table.column("value").type == pa.list_(pa.struct([pa.field("moment", pa.string())]))
    assert hive_stage_type(target_type) == "ARRAY(ROW(moment VARCHAR))"
    data = io.BytesIO()
    pq.write_table(table, data)
    assert pq.read_table(pa.BufferReader(data.getvalue())).column("value").to_pylist() == [
        [{"moment": "2024-01-01T00:00:00.000001000" + ("+00:00" if aware else "")}],
        None,
    ]
    # Actual finalization casts the VARCHAR stage fields back into the native
    # destination type without changing the connector or its table format.
    insert_sql = get_backend_adapter("trino").build_insert_from_table_sql(
        "iceberg.analytics.target", "hive.staging.source", {"value": target_type}
    )
    inserted_type = parse_one(insert_sql, read="trino").find(exp.Cast).args["to"]
    assert inserted_type.sql(dialect="trino") == f"ARRAY(ROW(moment {temporal_type}))"


def test_explicit_lower_temporal_precision_retains_deliberate_conversion() -> None:
    value = pd.Timestamp("2024-01-01T00:00:00.123456789")
    table = row_batch_to_arrow_table(
        pa, RowBatch(["value"], [([value],)]), column_types={"value": "ARRAY(TIMESTAMP(6))"}
    )
    assert table.column("value").to_pylist() == [[value.to_pydatetime(warn=False)]]


@pytest.mark.parametrize(
    "native", ["ARRAY(BIGINT, BIGINT)", "MAP(BIGINT)", "MAP(BIGINT, BIGINT, BIGINT)"]
)
def test_invalid_native_container_arity_fails_before_arrow_inference(native: str) -> None:
    with pytest.raises(ValueError, match="Invalid nested Trino type"):
        arrow_type_for_trino_stage(pa, native)
    with pytest.raises(ValueError, match="Invalid nested Trino type"):
        hive_stage_type(native)

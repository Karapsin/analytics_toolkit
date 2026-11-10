from __future__ import annotations

import io
from decimal import Decimal
from uuid import UUID

import fsspec
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.backends.trino.parquet_stage import _infer_trino_type_from_values
from analytics_toolkit.sql.dml.transfer.flow.parquet_stage import (
    row_batch_to_arrow_table,
    write_batch_to_parquet_stage,
    write_dataframe_to_parquet_stage,
)
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch
from analytics_toolkit.sql.dml.transfer.schema import map_source_schema_to_target


def exact_batch():
    return RowBatch(
        ["id", "amount", "uuid", "event_at"],
        [
            (
                2**53 + 1,
                Decimal("12345678901234567890.123456789"),
                UUID(int=1),
                pd.Timestamp("2024-01-01T03:00:00.123456+03:00"),
            ),
            (None, None, None, None),
        ],
    )


TYPES = {
    "id": "BIGINT",
    "amount": "DECIMAL(29, 9)",
    "uuid": "UUID",
    "event_at": "TIMESTAMP(6) WITH TIME ZONE",
}


def assert_exact(table):
    assert table.schema.types == [pa.int64(), pa.decimal128(29, 9), pa.string(), pa.string()]
    assert table.column("id").to_pylist() == [2**53 + 1, None]
    assert table.column("amount").to_pylist() == [Decimal("12345678901234567890.123456789"), None]
    assert table.column("uuid").to_pylist() == [str(UUID(int=1)), None]
    assert table.column("event_at").to_pylist() == ["2024-01-01T00:00:00.123456+00:00", None]


def test_every_parquet_column_uses_resolved_physical_stage_type() -> None:
    table = row_batch_to_arrow_table(pa, exact_batch(), column_types=TYPES)
    data = io.BytesIO()
    pq.write_table(table, data)
    assert_exact(pq.read_table(pa.BufferReader(data.getvalue())))
    for rows in ([(None,) * 4], []):
        empty = row_batch_to_arrow_table(pa, RowBatch(list(TYPES), rows), column_types=TYPES)
        assert empty.schema.types == table.schema.types


@pytest.mark.parametrize("dataframe", [False, True])
def test_dataframe_and_row_writers_apply_identical_uuid_timezone_and_numeric_schema(
    dataframe: bool,
) -> None:
    location = "memory://exact-parquet/" + ("df" if dataframe else "rows")
    kwargs = {
        "stage_external_location": location,
        "pa": pa,
        "pq": pq,
        "fsspec_module": fsspec,
        "row_group_size": 1,
        "column_types": TYPES,
    }
    if dataframe:
        batch = exact_batch()
        # Object columns are intentional: the input may be built without lossy
        # pandas inference and the writer must never introduce it.
        frame = pd.DataFrame(
            {
                name: pd.Series([row[index] for row in batch.rows], dtype=object)
                for index, name in enumerate(batch.columns)
            }
        )
        assert write_dataframe_to_parquet_stage(frame, **kwargs) == 2
        tables = [
            pq.read_table(
                pa.BufferReader(
                    fsspec.open(f"{location}/part-{index:05d}.parquet", "rb").open().read()
                )
            )
            for index in range(2)
        ]
        assert_exact(pa.concat_tables(tables))
    else:
        assert write_batch_to_parquet_stage(exact_batch(), file_index=0, **kwargs) == 2
        with fsspec.open(f"{location}/part-00000.parquet", "rb") as stream:
            assert_exact(pq.read_table(pa.BufferReader(stream.read())))


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([None, Decimal("0.0001")], "DECIMAL(4, 4)"),
        ([Decimal("1E+5")], "DECIMAL(6, 0)"),
        ([Decimal("1.2"), Decimal("0.0034")], "DECIMAL(5, 4)"),
        ([Decimal(1), Decimal("1E+38")], "VARCHAR"),
        ([1, 2**64 - 1], "DECIMAL(20, 0)"),
    ],
)
def test_parquet_fallback_inference_scans_the_whole_column(values, expected):
    assert _infer_trino_type_from_values(iter(values)) == expected


def test_same_trino_quoted_row_fields_survive_actual_parquet_round_trip() -> None:
    native = 'ROW("MixedCase" BIGINT, "Spaced Name" ARRAY(UUID))'
    types = map_source_schema_to_target(
        [SourceColumn("payload", native)], "trino", source_backend="trino"
    )
    batch = RowBatch(["payload"], [({"MixedCase": 2**53 + 1, "Spaced Name": [UUID(int=1), None]},)])
    table = row_batch_to_arrow_table(pa, batch, column_types=types)
    assert table.schema.field("payload").type.names == ["MixedCase", "Spaced Name"]
    target = io.BytesIO()
    pq.write_table(table, target)
    recovered = pq.read_table(pa.BufferReader(target.getvalue()))
    assert recovered.to_pylist() == [
        {"payload": {"MixedCase": 2**53 + 1, "Spaced Name": [str(UUID(int=1)), None]}}
    ]

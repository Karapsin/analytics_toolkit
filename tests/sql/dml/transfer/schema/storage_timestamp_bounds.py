"""Prospective SQL stage and final table storage have independent timestamp limits."""

from __future__ import annotations

import datetime as dt
import io
from dataclasses import replace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from analytics_toolkit.sql.backends.calendar_ticks import days_from_civil
from analytics_toolkit.sql.backends.trino.arrow_types import hive_stage_type
from analytics_toolkit.sql.core.exact_values import ExactTemporal
from analytics_toolkit.sql.dml.transfer.flow.parquet_stage import row_batch_to_arrow_table
from analytics_toolkit.sql.dml.transfer.runtime.models import RowBatch

from tests.sql._support import adaptive_transfer as support


def _install(monkeypatch, *, stage_connector, final_connector):
    catalogs = {"stage_catalog": stage_connector, "target_catalog": final_connector}
    statements = []

    class Cursor:
        def execute(self, statement, params=None):
            statements.append((statement, list(params or [])))
            if "system.metadata.catalogs" in statement:
                assert len(params) == 1
                self.rows = [(catalogs[params[0]],)]
            elif "SHOW CREATE TABLE" in statement:
                self.rows = [
                    (
                        "CREATE TABLE target_catalog.sandbox.target (id TIMESTAMP(9)) "
                        "WITH (format_version = 3, format = 'PARQUET')",
                    )
                ]
            else:
                message = f"Unexpected timestamp capability metadata: {statement}"
                raise AssertionError(message)

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Connection(support.Connection):
        catalog = "target_catalog"
        schema = "sandbox"

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(support, "Connection", Connection)
    wide = ExactTemporal(days_from_civil(10000, 1, 1) * 86400 * 10**9 + 1, 9, wall_time=True)
    scenario = support.install(
        monkeypatch,
        native_type="TIMESTAMP(12)",
        rows=[(None,), (ExactTemporal(0, 9, wall_time=True),), (wide,)],
        from_db_key="trino",
        from_db_backend="trino",
        to_db_key="trino",
        to_db_backend="trino",
        target_table="target_catalog.sandbox.target",
        table_schema={"id": "TIMESTAMP(9)"},
        transfer_staging_schema="stage_catalog.staging",
        staging_ddl_properties={"format_version": 3},
        regular_ddl_properties={"format_version": 3},
    )
    return scenario, wide, statements


@pytest.mark.parametrize(
    ("stage_connector", "final_connector"),
    [("iceberg", "memory"), ("memory", "iceberg")],
)
def test_public_transfer_rejects_wide_timestamp_in_actual_stage_or_final_storage_before_create(
    monkeypatch,
    stage_connector,
    final_connector,
):
    scenario, _wide, statements = _install(
        monkeypatch,
        stage_connector=stage_connector,
        final_connector=final_connector,
    )
    paths = support.track_spools(monkeypatch)

    try:
        with pytest.raises(ValueError, match=r"(?i)timestamp|nanosecond|storage"):
            scenario.run()
    finally:
        assert "create_target" not in scenario.events
        assert scenario.loaded == []
        assert scenario.finalizations == 0

    assert scenario.read_count == 1
    assert any("system.metadata.catalogs" in statement for statement, _params in statements)
    assert all(not path.exists() for path in paths)


def test_public_transfer_keeps_exact_wide_timestamp_with_independently_verified_memory_storage(
    monkeypatch,
):
    scenario, wide, statements = _install(
        monkeypatch,
        stage_connector="memory",
        final_connector="memory",
    )

    assert scenario.run() == 3

    catalogs = {
        params[0] for statement, params in statements if "system.metadata.catalogs" in statement
    }
    assert catalogs == {"stage_catalog", "target_catalog"}
    assert scenario.events.index("complete:SELECT id FROM source") < (
        scenario.events.index("create_target")
    )
    epoch = dt.datetime.combine(dt.date(1970, 1, 1), dt.time())
    assert scenario.loaded == [[(None,), (epoch,), (wide,)]]
    assert scenario.read_count == 1


def test_public_parquet_transfer_skips_native_hive_string_guard_and_keeps_final_timestamp_proof(
    monkeypatch,
):
    scenario, wide, statements = _install(
        monkeypatch,
        stage_connector="hive",
        final_connector="memory",
    )
    scenario.options = replace(
        scenario.options,
        trino_mode="parquet",
        s3_transfer_staging_schema="stage_catalog.staging",
    )
    load = support.attempt.load_stage_batches
    files = []

    def write(options, refs, state, *args, **kwargs):
        physical = {
            name: hive_stage_type(native) for name, native in state.stage_column_types.items()
        }
        state.stage_column_types = physical
        count = load(options, refs, state, *args, **kwargs)
        table = row_batch_to_arrow_table(
            pa,
            RowBatch(["id"], scenario.loaded[-1]),
            column_types=physical,
        )
        output = io.BytesIO()
        pq.write_table(table, output)
        files.append(pq.read_table(io.BytesIO(output.getvalue())))
        return count

    monkeypatch.setattr(support.attempt, "load_stage_batches", write)

    assert scenario.run() == 3

    assert scenario.schemas == [{"id": "VARCHAR"}]
    assert files[0].schema.field("id").type == pa.string()
    assert files[0].column("id").to_pylist()[0] is None
    assert files[0].column("id").to_pylist()[2] == wide.isoformat()
    assert {
        params[0] for statement, params in statements if "system.metadata.catalogs" in statement
    } == {"target_catalog"}
    assert scenario.options.input_resolution["resolved_logical_schema"] == {"id": "TIMESTAMP(9)"}
    assert scenario.read_count == 1

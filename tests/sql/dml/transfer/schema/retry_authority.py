"""A certified intermediate cannot bypass refreshed authoritative destination types."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from sqlglot import exp

from tests.sql._support.transfer_native_assignment import (
    NativeTransferConnection,
    NativeTransferDatabase,
    install_native_transfer,
)


def test_public_certified_retry_revalidates_existing_schema_before_reclearing_target(monkeypatch):
    assign = NativeTransferDatabase.assign

    def native_assignment(value, native, *, explicit=False):
        declared = exp.DataType.build(native, dialect="postgres")
        if declared.this == exp.DataType.Type.VARCHAR and declared.expressions:
            if value is None:
                return None
            text = str(value)
            width = int(declared.expressions[0].this.name)
            if not explicit and len(text) > width:
                message = "Native assignment exceeds VARCHAR width"
                raise ValueError(message)
            return text[:width]
        return assign(value, native, explicit=explicit)

    monkeypatch.setattr(NativeTransferDatabase, "assign", staticmethod(native_assignment))
    rows = [("abcdef",)]
    database = install_native_transfer(
        monkeypatch,
        rows=rows,
        target_schema={"payload": "TEXT"},
        source_types={"payload": "String"},
    )
    database.target.schema = {"payload": "VARCHAR(10)"}
    database.target.rows = [{"payload": "old"}]
    database.fail_target_ack = 1
    commit = NativeTransferConnection.commit
    observed_change = []

    def change_schema_after_lost_ack(connection):
        try:
            commit(connection)
        except ConnectionError:
            if not observed_change:
                database.target.schema = {"payload": "VARCHAR(3)"}
                database.target.rows = [{"payload": "new"}]
                observed_change.append(len(database.live_mutations))
            raise

    monkeypatch.setattr(NativeTransferConnection, "commit", change_schema_after_lost_ack)

    with pytest.raises(
        ValueError, match=r"authoritative text length|[Dd]estination schema differs"
    ):
        sql.transfer(
            "ch",
            "gp",
            from_sql="SELECT payload FROM source",
            to_table="sandbox.events",
            table_schema={"payload": "TEXT"},
            write_mode="truncate_insert",
            ignore_source_staging=True,
            validate_row_count=False,
            retry_cnt=1,
            full_retry_cnt=2,
            timeout_increment=0,
            full_timeout_increment=0,
        )

    assert observed_change
    assert database.target.schema == {"payload": "VARCHAR(3)"}
    assert database.target.rows == [{"payload": "new"}]
    assert len(database.live_mutations) == observed_change[0]
    assert len(database.reads) == 1
    assert database.source_opens == 1
    assert rows == [("abcdef",)]

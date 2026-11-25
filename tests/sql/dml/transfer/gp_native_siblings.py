"""An opaque GP column does not disable supported final casts of sibling columns."""

from __future__ import annotations

from analytics_toolkit import sql
from sqlglot import exp

from tests.sql._support.transfer_native_assignment import (
    CHECKED_DOMAIN,
    NativeTransferDatabase,
    install_native_transfer,
)


def test_public_native_assignment_keeps_supported_text_boolean_sibling_conversion(monkeypatch):
    assign = NativeTransferDatabase.assign
    seed = NativeTransferDatabase.seed
    decode = NativeTransferDatabase.decode_value

    def assignment(value, native, *, explicit=False):
        if native.upper() == "BOOLEAN":
            if value is None or type(value) is bool:
                return value
            assert isinstance(value, str)
            # This fake models only the two native tokens used by this case.
            assert value in {"true", "false"}
            return value == "true"
        return assign(value, native, explicit=explicit)

    def decode_value(cls, node, native_type):
        if native_type.upper() == "BOOLEAN" and isinstance(node, exp.Boolean):
            return node.this
        return decode(node, native_type)

    monkeypatch.setattr(NativeTransferDatabase, "assign", staticmethod(assignment))
    monkeypatch.setattr(
        NativeTransferDatabase,
        "seed",
        classmethod(lambda _cls, native: True if native.upper() == "BOOLEAN" else seed(native)),
    )
    monkeypatch.setattr(NativeTransferDatabase, "decode_value", classmethod(decode_value))
    rows = [("ok", "false")]
    database = install_native_transfer(
        monkeypatch,
        rows=rows,
        target_schema={"opaque": CHECKED_DOMAIN, "flag": "BOOLEAN"},
        source_types={"opaque": "String", "flag": "String"},
    )

    result = sql.transfer(
        "ch",
        "gp",
        from_sql="SELECT opaque, flag FROM source",
        to_table="sandbox.events",
        table_schema={"opaque": CHECKED_DOMAIN, "flag": "TEXT"},
        write_mode="truncate_insert",
        ignore_source_staging=True,
        validate_row_count=False,
        retry_cnt=1,
        full_retry_cnt=1,
        timeout_increment=0,
        full_timeout_increment=0,
    )

    assert type(result) is int
    assert result == 1
    assert database.target.schema == {"opaque": CHECKED_DOMAIN, "flag": "BOOLEAN"}
    assert database.target.rows == [{"opaque": "ok", "flag": False}]
    assert type(database.target.rows[0]["flag"]) is bool
    assert len(database.reads) == database.source_opens == 1
    assert rows == [("ok", "false")]
    assert set(database.tables) == {("sandbox", "events")}

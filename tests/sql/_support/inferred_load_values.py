"""Native numeric/text storage reached through the real psycopg writer."""

from __future__ import annotations

from decimal import Decimal, localcontext

from sqlglot import exp

from tests.sql._support.gp_dates import DateDatabase, _DateConnection
from tests.sql._support.gp_relation_identity import RelationIdentityDatabase
from tests.sql._support.load_table import load_df_module


class InferredValuesDatabase(RelationIdentityDatabase, DateDatabase):
    def __init__(self, native_type, *, exists):
        super().__init__()
        self.target.schema = {"payload": native_type}
        self.target.rows = [{"payload": Decimal(7)}]
        if not exists:
            self.tables.clear()

    @staticmethod
    def decode_value(node, native_type):
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        if native_type.upper() == "TEXT":
            assert isinstance(node, exp.Literal)
            assert node.is_string
            return node.this
        negative = isinstance(node, exp.Neg)
        if negative:
            node = node.this
        assert isinstance(node, exp.Literal)
        assert not node.is_string
        value = Decimal(node.this)
        if negative:
            value = -value
        parsed = exp.DataType.build(native_type, dialect="postgres")
        assert parsed.this == exp.DataType.Type.DECIMAL
        precision, scale = (int(parameter.this.this) for parameter in parsed.expressions)
        with localcontext() as context:
            context.prec = precision
            return value.quantize(Decimal(1).scaleb(-scale))


def inferred_values_target(monkeypatch, *, native_type="NUMERIC(71,55)", exists=False):
    database = InferredValuesDatabase(native_type, exists=exists)

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database

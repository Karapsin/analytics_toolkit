"""Actual psycopg binding with independently modeled native scalar assignments."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from sqlglot import exp, parse_one

from tests.sql._support.gp_dates import _DateConnection
from tests.sql._support.gp_numeric_text import NumericDatabase, _literal_text
from tests.sql._support.gp_relation_identity import RelationIdentityDatabase
from tests.sql._support.load_table import load_df_module


class NativeScalarDatabase(RelationIdentityDatabase, NumericDatabase):
    def __init__(self, native):
        super().__init__(native, exists=True)
        self.target.rows = [{"payload": 101 if native == "BIGINT" else Decimal("101.25")}]
        self.settings = {
            "timezone": "UTC",
            "datestyle": "ISO, MDY",
            "timezone_abbreviations": "Default",
            "intervalstyle": "postgres",
        }

    def metadata(self, statement, params):
        if "current_setting" in statement.lower():
            tree = parse_one(statement, read="postgres")
            names = [
                node.expressions[0].this.lower()
                for node in tree.find_all(exp.Anonymous)
                if node.name.lower() == "current_setting"
            ]
            return [tuple(self.settings[name] for name in names)]
        return super().metadata(statement, params)

    def _insert(self, tree, identity, params):
        if isinstance(tree.expression, exp.Values):
            return super()._insert(tree, identity, params)
        names = [column.name for column in tree.this.expressions]
        schema = self.tables[identity].schema
        selected, _columns = self.select(tree.expression)
        rows = [
            tuple(self.assign(value, schema[name]) for name, value in zip(names, row))
            for row in selected
        ]
        candidate = deepcopy(self.tables[identity])
        candidate.insert(names, rows)
        self.tables[identity] = candidate
        if identity != ("sandbox", "events"):
            self.stage_inserts.append((identity, rows))
        return None

    def value(self, node, row):
        if isinstance(node, exp.Cast):
            return self.assign(self.value(node.this, row), node.args["to"].sql(dialect="postgres"))
        return super().value(node, row)

    @classmethod
    def assign(cls, value, native):
        if value is None:
            return None
        return cls.decode_value(exp.Literal.string(str(value)), native)

    @staticmethod
    def decode_value(node, native_type):
        text = _literal_text(node)
        if text is None:
            return None
        native = native_type.upper()
        if native == "BIGINT":
            value = Decimal(text)
            if value != value.to_integral_value() or not -(2**63) <= value < 2**63:
                message = "Native integer assignment rejected."
                raise ValueError(message)
            return int(value)
        if text.lower() in {"nan", "infinity", "+infinity", "-infinity"}:
            if native.startswith(("NUMERIC", "DECIMAL")):
                if text.lower() != "nan":
                    message = "Native numeric assignment is unsupported by this GP server version."
                    raise ValueError(message)
                return Decimal("NaN")
            return float(text)
        return NumericDatabase.decode_value(node, native_type)


def native_scalar_target(monkeypatch, native):
    database = NativeScalarDatabase(native)

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database

"""Native key checks over real psycopg VALUES and stateful typed INSERT SELECT."""

from __future__ import annotations

from copy import deepcopy

from sqlglot import exp

from tests.sql._support.gp_booleans import BooleanDatabase
from tests.sql._support.gp_dates import DateDatabase, _DateConnection
from tests.sql._support.gp_integers import IntegerDatabase
from tests.sql._support.load_table import load_df_module


class _KeyPublication:
    def select(self, tree, outer=None):
        joins = tree.args.get("joins")
        if not joins:
            return super().select(tree, outer)
        assert outer is None
        assert tree.args.get("group") is None
        assert tree.args.get("where") is None
        source = tree.args["from_"].this
        rows = self._aliased_rows(source)
        for join in joins:
            assert join.args.get("kind") == "INNER"
            assert join.args.get("side") is None
            joined = self._aliased_rows(join.this)
            rows = [
                {**left, **right}
                for left in rows
                for right in joined
                if self.matches(join.args["on"], {**left, **right})
            ]
        values = [tuple(self.value(column, row) for column in tree.expressions) for row in rows]
        limit = tree.args.get("limit")
        if limit is not None:
            values = values[: int(limit.expression.this)]
        return values, [column.alias_or_name for column in tree.expressions]

    def _aliased_rows(self, table):
        return [
            {**row, **{f"{table.alias_or_name}.{name}": value for name, value in row.items()}}
            for row in self.table(table).rows
        ]

    def _insert(self, tree, identity, params):
        if isinstance(tree.expression, exp.Values):
            super()._insert(tree, identity, params)
        else:
            assert params is None
            assert isinstance(tree.expression, exp.Select)
            candidate = deepcopy(self.tables[identity])
            names = [column.name for column in tree.this.expressions] or list(candidate.schema)
            rows, _columns = self.select(tree.expression)
            candidate.insert(
                names,
                [
                    tuple(
                        _assign_selected_value(value, candidate.schema[name])
                        for value, name in zip(row, names)
                    )
                    for row in rows
                ],
            )
            self.tables[identity] = candidate
        if identity != ("sandbox", "events"):
            table = self.tables[identity]
            self.staged_rows.append((dict(table.schema), deepcopy(table.rows)))


class _IntegerKeys(_KeyPublication, IntegerDatabase):
    pass


class _BooleanKeys(_KeyPublication, BooleanDatabase):
    pass


class _StringKeys(_KeyPublication, DateDatabase):
    def __init__(self, native_type):
        super().__init__()
        self.target.schema = {"payload": native_type}

    @staticmethod
    def decode_value(node, native_type):
        while isinstance(node, exp.Cast):
            node = node.this
        if isinstance(node, exp.Null):
            return None
        assert isinstance(node, exp.Literal)
        assert node.is_string
        return _assign_selected_value(node.this, native_type)


def _assign_selected_value(value, native_type):
    if value is None:
        return None
    kind = exp.DataType.build(native_type, dialect="postgres")
    if kind.this in {exp.DataType.Type.SMALLINT, exp.DataType.Type.INT, exp.DataType.Type.BIGINT}:
        if not isinstance(value, int) or isinstance(value, bool):
            message = "Server rejected the typed source's native integer assignment."
            raise ValueError(message)
        return IntegerDatabase.decode_value(exp.Literal.number(str(value)), native_type)
    if kind.this == exp.DataType.Type.BOOLEAN:
        assert isinstance(value, bool)
        return value
    assert kind.this in {exp.DataType.Type.CHAR, exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT}
    assert isinstance(value, str)
    if kind.expressions:
        width = int(kind.expressions[0].this.name)
        if len(value) > width:
            message = "Server rejected the typed source's character assignment width."
            raise ValueError(message)
        if kind.this == exp.DataType.Type.CHAR:
            return value.ljust(width)
    return value


def effective_key_target(monkeypatch, native_type, *, empty=False):
    if native_type == "BOOLEAN":
        database = _BooleanKeys()
        seed = True
    elif native_type.startswith(("CHAR", "VARCHAR", "TEXT")):
        database = _StringKeys(native_type)
        seed = _assign_selected_value("z", native_type)
    else:
        database = _IntegerKeys(native_type, exists=True)
        seed = 101
    database.staged_rows = []
    database.target.rows = [] if empty else [{"payload": seed}]

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database

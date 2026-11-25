"""Small stateful driver for partition-upsert SQL emitted by the real adapter."""

from __future__ import annotations

import operator
from collections import defaultdict
from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import Dialect, exp, parse_one

from tests.sql._support.upsert_metadata import UpsertMetadata

COLUMNS = ("id", "month", "value")
TARGET_ROWS = [(1, 202401, 10), (2, 202401, 20), (1, 202402, 30), (3, 202403, 40)]
INCOMING_ROWS = [(1, 202401, 11), (1, 202402, 31)]
UNRELATED_ROWS = [(1, 202401, 900), (8, 202401, 800), (1, 202402, 700)]
EXPECTED_ROWS = [(1, 202401, 11), (2, 202401, 20), (1, 202402, 31), (3, 202403, 40)]
_UNKNOWN = object()
_BINARY = {
    exp.And: operator.and_,
    exp.Or: operator.or_,
    exp.EQ: operator.eq,
    exp.NullSafeEQ: operator.eq,
    exp.Is: operator.eq,
    exp.NEQ: operator.ne,
    exp.GT: operator.gt,
    exp.LT: operator.lt,
    exp.Sub: operator.sub,
}


class ScopedRows(dict):
    """Keep old default-database fixture keys while retaining every real namespace."""

    def __init__(self, values):
        super().__init__()
        for key, rows in values.items():
            self[key] = rows

    @staticmethod
    def _key(key):
        return (key[0], "db", key[1]) if len(key) == 2 else key

    def __getitem__(self, key):
        return super().__getitem__(self._key(key))

    def __setitem__(self, key, value):
        super().__setitem__(self._key(key), value)


class StatefulUpsertClient:
    def __init__(self, *, local):
        self.local = local
        self.target = (None, "events") if local else ("storage_b", "events_shard")
        self.unrelated = ("caller_a", "events" if local else "events_shard")
        self.rows = ScopedRows(
            {
                self.target: list(TARGET_ROWS),
                self.unrelated: list(UNRELATED_ROWS),
                ("caller_a", "incoming"): list(INCOMING_ROWS),
                ("caller_a", "payload"): [],
            }
        )
        self.commands = []
        self.queries = []
        self.metadata = UpsertMetadata(local=local)

    def _ddl(self, table):
        engine = (
            "Distributed('storage_b', 'db', 'events_shard', rand())"
            if table == "events" and not self.local
            else "MergeTree PARTITION BY month ORDER BY id"
        )
        return f"CREATE TABLE db.{table} (id Int64, month Int64, value Int64) ENGINE={engine}"

    def query(self, statement, settings=None, **_kwargs):
        self.queries.append(statement)
        metadata = self.metadata.query(statement, settings=settings)
        if metadata is not None:
            return metadata
        tree = parse_one(statement, read=ClickHouseDialect)
        return self._result(self._select(tree))

    @staticmethod
    def _result(rows):
        return SimpleNamespace(result_rows=rows)

    def _identity(self, source):
        if isinstance(source, exp.Subquery):
            return self._identity(source.this.args["from_"].this)
        function = source.this
        if isinstance(function, exp.Anonymous):
            scope, database, table = [str(value.this) for value in function.expressions[:3]]
        else:
            scope, database, table = None, source.db or "db", source.name
        if (
            table == "events"
            and database == "db"
            and not self.local
            and scope in {None, "caller_a"}
        ):
            return "storage_b", database, "events_shard"
        return scope, database, table

    def _source_rows(self, source):
        if isinstance(source, exp.Subquery):
            return self._select(source.this)
        return list(self.rows[self._identity(source)])

    def _select(self, tree):
        return self._select_data(tree)[0]

    def _source_data(self, source):
        return (
            self._select_data(source.this)
            if isinstance(source, exp.Subquery)
            else (self._source_rows(source), COLUMNS)
        )

    def _records(self, source):
        rows, columns = self._source_data(source)
        alias = source.alias_or_name
        return [
            {
                **dict(zip(columns, row)),
                **{f"{alias}.{name}": value for name, value in zip(columns, row)},
            }
            for row in rows
        ]

    def _select_data(self, tree):
        source = tree.args["from_"].this
        records = self._records(source)
        for join in tree.args.get("joins", []):
            records = self._join_records(records, join)
        records = [row for row in records if self._value(tree.args.get("where"), row)]
        records = self._group_records(records, tree)
        records = [row for row in records if self._value(tree.args.get("having"), row)]
        _rows, source_columns = self._source_data(source)
        columns = (
            source_columns
            if isinstance(tree.expressions[0], exp.Star)
            else [item.alias_or_name for item in tree.expressions]
        )
        selected = [
            tuple(row[name] for name in columns)
            if isinstance(tree.expressions[0], exp.Star)
            else tuple(self._value(item, row) for item in tree.expressions)
            for row in records
        ]
        if tree.args.get("distinct"):
            selected = list(dict.fromkeys(selected))
        if limit := tree.args.get("limit"):
            selected = selected[: int(limit.expression.this)]
        return selected, columns

    def _join_records(self, records, join):
        rights = self._records(join.this)
        joined = []
        for left in records:
            matches = [
                {**right, **left}
                for right in rights
                if self._value(join.args.get("on"), {**right, **left})
            ]
            if str(join.args.get("kind", "")).upper() == "ANTI":
                if not matches:
                    joined.append(left)
            else:
                joined.extend(matches)
                if not matches and str(join.args.get("side", "")).upper() == "LEFT":
                    joined.append(left)
        return joined

    def _group_records(self, records, tree):
        if group := tree.args.get("group"):
            grouped = defaultdict(list)
            for row in records:
                grouped[tuple(self._value(item, row) for item in group.expressions)].append(row)
            return [{**rows[0], "__group_rows__": rows} for rows in grouped.values()]
        if any(item.find(exp.Count) is not None for item in tree.expressions):
            return [{"__group_rows__": records}]
        return records

    @staticmethod
    def _scalar(node, row):
        if node is None:
            return True
        if isinstance(node, exp.Column):
            return row[f"{node.table}.{node.name}" if node.table else node.name]
        if isinstance(node, exp.Literal):
            return node.to_py()
        if isinstance(node, exp.Null):
            return None
        if isinstance(node, exp.Boolean):
            return node.this
        return _UNKNOWN

    def _value(self, node, row):
        scalar = self._scalar(node, row)
        if scalar is not _UNKNOWN:
            return scalar
        if isinstance(node, (exp.Where, exp.Having, exp.Paren, exp.Alias, exp.Cast)):
            return self._value(node.this, row)
        if isinstance(node, exp.Count):
            rows = row["__group_rows__"]
            return (
                len(rows)
                if isinstance(node.this, exp.Star)
                else sum(self._value(node.this, item) is not None for item in rows)
            )
        if isinstance(node, exp.Not):
            return not self._value(node.this, row)
        if operation := _BINARY.get(type(node)):
            return operation(self._value(node.left, row), self._value(node.right, row))
        if isinstance(node, exp.Anonymous) and node.name.lower() == "isnotdistinctfrom":
            left, right = node.expressions
            return self._value(left, row) == self._value(right, row)
        message = f"Unsupported stateful upsert expression: {node}"
        raise AssertionError(message)

    @staticmethod
    def _insert_parts(statement):
        tokens = Dialect.get_or_raise(ClickHouseDialect).tokenize(statement)
        select = next(token for token in tokens if token.text.upper() == "SELECT")
        start = (
            next(index for index, token in enumerate(tokens) if token.text.upper() == "INTO") + 1
        )
        function = tokens[start].text.upper() == "FUNCTION"
        if function:
            start += 1
            depth = 0
            end = start
            for index in range(start + 1, len(tokens)):
                depth += int(tokens[index].text == "(") - int(tokens[index].text == ")")
                if depth == 0:
                    end = index
                    break
        else:
            end = start
            while tokens[end + 1].text == ".":
                end += 2
        target_sql = statement[tokens[start].start : tokens[end].end + 1]
        target = parse_one("SELECT * FROM " + target_sql, read=ClickHouseDialect).args["from_"].this
        query = parse_one(statement[select.start :], read=ClickHouseDialect)
        return target, query

    def command(self, statement, **_kwargs):
        self.commands.append(statement)
        if statement.startswith("INSERT INTO"):
            target, query = self._insert_parts(statement)
            insert = parse_one(statement, read=ClickHouseDialect)
            names = (
                [column.name for column in insert.this.expressions]
                if isinstance(insert.this, exp.Schema)
                else COLUMNS
            )
            self.rows[self._identity(target)].extend(
                tuple(dict(zip(names, row))[name] for name in COLUMNS)
                for row in self._select(query)
            )
        elif statement.startswith("ALTER TABLE"):
            tree = parse_one(statement, read=ClickHouseDialect)
            scope = tree.args["cluster"].this.name if tree.args.get("cluster") else None
            partition = int(tree.find(exp.DropPartition).expressions[0].expressions[0].this)
            identity = scope, tree.this.db or "db", tree.this.name
            self.rows[identity] = [row for row in self.rows[identity] if row[1] != partition]

    def insert_df(self, table, df, **_kwargs):
        target = parse_one("SELECT * FROM " + table, read=ClickHouseDialect).args["from_"].this
        scope, database, name = self._identity(target)
        self.rows[(scope or "caller_a", database, name)] = [
            tuple(dict(zip(df.columns, row))[column] for column in COLUMNS)
            for row in df.itertuples(index=False, name=None)
        ]

    def close(self):
        pass

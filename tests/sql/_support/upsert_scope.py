"""Small stateful driver for partition-upsert SQL emitted by the real adapter."""

from __future__ import annotations

from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import Dialect, exp, parse_one

COLUMNS = ("id", "month", "value")
TARGET_ROWS = [(1, 202401, 10), (2, 202401, 20), (1, 202402, 30), (3, 202403, 40)]
INCOMING_ROWS = [(1, 202401, 11), (1, 202402, 31)]
UNRELATED_ROWS = [(1, 202401, 900), (8, 202401, 800), (1, 202402, 700)]
EXPECTED_ROWS = [(1, 202401, 11), (2, 202401, 20), (1, 202402, 31), (3, 202403, 40)]


class StatefulUpsertClient:
    def __init__(self, *, local):
        self.local = local
        self.target = (None, "events") if local else ("storage_b", "events_shard")
        self.unrelated = ("caller_a", "events" if local else "events_shard")
        self.rows = {
            self.target: list(TARGET_ROWS),
            self.unrelated: list(UNRELATED_ROWS),
            ("caller_a", "incoming"): list(INCOMING_ROWS),
            ("caller_a", "payload"): [],
        }
        self.commands = []
        self.queries = []

    def _ddl(self, table):
        engine = (
            "Distributed('storage_b', 'db', 'events_shard', rand())"
            if table == "events" and not self.local
            else "MergeTree PARTITION BY month ORDER BY id"
        )
        return f"CREATE TABLE db.{table} (id Int64, month Int64, value Int64) ENGINE={engine}"

    def query(self, statement):
        self.queries.append(statement)
        if statement.startswith("SHOW CREATE TABLE"):
            table = (
                statement.removeprefix("SHOW CREATE TABLE ")
                .replace("`", "")
                .replace('"', "")
                .split(".")[-1]
            )
            return self._result([(self._ddl(table),)])
        if statement.startswith("DESCRIBE TABLE"):
            return self._result([(column, "Int64", "") for column in COLUMNS])
        if statement.startswith("EXISTS TABLE"):
            return self._result([(1,)])
        tree = parse_one(statement, read=ClickHouseDialect)
        filters = {
            item.left.name: str(item.right.this)
            for item in tree.find_all(exp.EQ)
            if isinstance(item.right, exp.Literal)
        }
        relation = filters.get("name", "events")
        functions = [
            node
            for node in tree.find_all(exp.Anonymous)
            if node.name.lower() in {"cluster", "clusterallreplicas"}
        ]
        cluster = (
            str(functions[0].expressions[0].this)
            if functions
            else filters.get("cluster", "caller_a")
        )
        host = "host_b" if cluster == "storage_b" else "host_a"
        if "FROM system.clusters" in statement:
            rows = [(1, 1, host)]
        elif statement.startswith("SELECT create_table_query FROM clusterAllReplicas("):
            rows = [(self._ddl(relation),)]
        elif "SELECT hostName()" in statement and "system, tables" in statement:
            kind = "Distributed" if relation == "events" and not self.local else "MergeTree"
            full = (
                "Distributed('storage_b', 'db', 'events_shard', rand())"
                if kind == "Distributed"
                else "MergeTree()"
            )
            rows = [(host, kind, full, self._ddl(relation), "fake-uuid")]
        elif "FROM system.tables" in statement:
            kind = "Distributed" if relation == "events" and not self.local else "MergeTree"
            full = (
                "Distributed('storage_b', 'db', 'events_shard', rand())"
                if kind == "Distributed"
                else "MergeTree()"
            )
            rows = [(kind, full, self._ddl(relation))]
        elif "system, one" in statement:
            rows = [(host,)]
        elif "HAVING" in statement:
            rows = []
        else:
            rows = self._select(tree)
        return self._result(rows)

    @staticmethod
    def _result(rows):
        return SimpleNamespace(result_rows=rows)

    def _identity(self, source):
        if isinstance(source, exp.Subquery):
            return self._identity(source.this.args["from_"].this)
        function = source.this
        if isinstance(function, exp.Anonymous):
            scope, _database, table = [str(value.this) for value in function.expressions[:3]]
        else:
            scope, table = None, source.name
        if table == "events" and not self.local:
            return "storage_b", "events_shard"
        return scope, table

    def _source_rows(self, source):
        if isinstance(source, exp.Subquery):
            return self._select(source.this)
        return list(self.rows[self._identity(source)])

    def _select(self, tree):
        rows = self._source_rows(tree.args["from_"].this)
        joins = tree.args.get("joins", [])
        if joins:
            partitions = {row[0] for row in self._source_rows(joins[0].this)}
            incoming = self._source_rows(joins[-1].this)
            keys = {(row[1], row[0]) for row in incoming}
            rows = [row for row in rows if row[1] in partitions and (row[1], row[0]) not in keys]
        selected = [
            tuple(
                row[COLUMNS.index(expression.find(exp.Column).name)]
                for expression in tree.expressions
            )
            for row in rows
        ]
        return list(dict.fromkeys(selected)) if tree.args.get("distinct") else selected

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
            self.rows[self._identity(target)].extend(self._select(query))
        elif statement.startswith("ALTER TABLE"):
            tree = parse_one(statement, read=ClickHouseDialect)
            scope = tree.args["cluster"].this.name if tree.args.get("cluster") else None
            partition = int(tree.find(exp.DropPartition).expressions[0].expressions[0].this)
            identity = scope, tree.this.name
            self.rows[identity] = [row for row in self.rows[identity] if row[1] != partition]

    def insert_df(self, table, df, **_kwargs):
        self.rows[("caller_a", table.split(".")[-1])] = list(df.itertuples(index=False, name=None))

    def close(self):
        pass

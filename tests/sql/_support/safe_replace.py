from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_statements import exchange_or_rename
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import retarget_create
from analytics_toolkit.sql.core.identifiers import TableIdentifier
from sqlglot import exp, parse_one

from tests.sql._support.ch_transfer_replacement import _selected_rows


class ReplacementCursor:
    def __init__(self, connection: TransactionalReplacementConnection) -> None:
        self.connection = connection

    def execute(self, sql: str) -> None:
        self.connection.executed.append(sql)
        if self.connection.fail_execute_at == len(self.connection.executed):
            message = "cutover failed"
            raise OSError(message)

    def close(self) -> None:
        return


class TransactionalReplacementConnection:
    def __init__(
        self,
        *,
        fail_commit: bool = False,
        fail_execute_at: int | None = None,
    ) -> None:
        self.executed: list[str] = []
        self.fail_commit = fail_commit
        self.commit_calls = 0
        self.rollback_calls = 0
        self.fail_execute_at = fail_execute_at

    def cursor(self) -> ReplacementCursor:
        return ReplacementCursor(self)

    def commit(self) -> None:
        self.commit_calls += 1
        if self.fail_commit:
            message = "connection lost during commit"
            raise OSError(message)

    def rollback(self) -> None:
        self.rollback_calls += 1


def _filters(sql: str) -> dict[str, str]:
    parsed = parse_one(sql, read=ClickHouseDialect)
    return {
        condition.this.name: condition.expression.this
        for condition in parsed.find_all(exp.EQ)
        if isinstance(condition.this, exp.Column) and isinstance(condition.expression, exp.Literal)
    }


class ClickHouseReplacementConnection:
    def __init__(self, *, recovery_artifacts: bool = True, pair: bool = True) -> None:
        self.queries: list[str] = []
        self.commands: list[str] = []
        self.query_settings: list[dict[str, Any] | None] = []
        self.recovery_artifacts = recovery_artifacts
        self.pair = pair
        self.members = ("h1", "h2") if pair else ("h1",)
        columns = "(id Int64, new_column String)"
        self.definitions = {
            "sandbox.target": (
                f"CREATE TABLE sandbox.target {columns} "
                "ENGINE = Distributed('core', 'sandbox', 'target_shard', rand())"
                if pair
                else f"CREATE TABLE sandbox.target {columns} ENGINE = MergeTree ORDER BY id"
            )
        }
        if pair:
            self.definitions["sandbox.target_shard"] = (
                f"CREATE TABLE sandbox.target_shard {columns} ENGINE = MergeTree ORDER BY id"
            )
        self.definitions = {
            name: parse_one(ddl, read=ClickHouseDialect).sql(dialect=ClickHouseDialect)
            for name, ddl in self.definitions.items()
        }
        self.table_identities = {name: uuid4() for name in self.definitions}
        self.table_rows = {name: [] for name in self.definitions}
        physical = "sandbox.target_shard" if pair else "sandbox.target"
        self.table_rows[physical] = [(901, "old-a"), (902, "old-b")]
        self.table_rows["stage.incoming"] = [(1, "a"), (2, "b")]
        self.input_columns = {"stage.incoming": ["id", "new_column"]}

    def _scope_metadata(self, sql: str) -> list[tuple[Any, ...]] | None:
        if sql.startswith("SELECT getMacro("):
            return [("core",)]
        if sql == "SELECT serverUUID(), tcpPort()":
            return [(str(UUID(int=1)), 9000)]
        if "system.clusters" in sql:
            return [
                (position, 1, host, f"10.0.0.{position}", 9000, int(position == 1))
                if "is_local" in sql
                else (position, 1, 0)
                for position, host in enumerate(self.members, 1)
            ]
        if "system, one" in sql and "serverUUID()" in sql:
            return [
                (position, str(UUID(int=position)), 9000)
                for position, _host in enumerate(self.members, 1)
            ]
        if "system, clusters" in sql:
            return [
                (position, "core", host, f"10.0.0.{target}", 9000)
                for position, _member in enumerate(self.members, 1)
                for target, host in enumerate(self.members, 1)
            ]
        return None

    def _table_metadata(self, sql: str) -> list[tuple[Any, ...]] | None:
        if sql.startswith("SHOW CREATE TABLE "):
            identifier = TableIdentifier.parse(sql[len("SHOW CREATE TABLE ") :], "ch")
            return [(self.definitions[".".join(identifier.parts)],)]
        parsed = parse_one(sql, read=ClickHouseDialect)
        if not isinstance(parsed, exp.Select) or parsed.find(exp.Count) is not None:
            return None
        source = parsed.args.get("from_")
        if source is None:
            return None
        return self._system_table_metadata(parsed, source.this, sql)

    def _system_table_metadata(
        self, parsed: exp.Select, source_table: exp.Table, sql: str
    ) -> list[tuple[Any, ...]] | None:
        system_table = source_table.name
        if isinstance(source_table.this, exp.Anonymous):
            system_table = source_table.this.expressions[2].name
        selected = (
            self.members if isinstance(source_table.this, exp.Anonymous) else self.members[:1]
        )
        if system_table == "server_settings":
            return [
                (_position(item, index), "/clickhouse/{database}/{table}/{shard}")
                for index, _host in enumerate(selected, 1)
                for item in [parsed.expressions[0]]
            ]
        if system_table == "macros":
            return [
                (_position(parsed.expressions[0], index), macro, value)
                for index, host in enumerate(selected, 1)
                for macro, value in (("shard", str(index)), ("replica", host))
            ]
        if system_table != "tables" or not any(
            item.name in {"uuid", "engine", "create_table_query"} for item in parsed.expressions
        ):
            return None
        return self._resource_metadata(parsed, selected, sql)

    def _resource_metadata(
        self, parsed: exp.Select, selected: tuple[str, ...], sql: str
    ) -> list[tuple[Any, ...]]:
        filters = _filters(sql)
        name = f"{filters['database']}.{filters['name']}"
        definition = self.definitions.get(name)
        if definition is None:
            return []
        create = parse_one(definition, read=ClickHouseDialect)
        record = {
            "uuid": self.table_identities[name],
            "engine": create.find(exp.EngineProperty).this.name,
            "create_table_query": definition,
        }
        return [
            tuple(
                index
                if item.name == "_shard_num"
                else _position(item, index)
                if isinstance(item, (exp.Alias, exp.Literal))
                else record[item.name]
                for item in parsed.expressions
            )
            for index, _host in enumerate(selected, 1)
        ]

    def _count(self, sql: str) -> int:
        filters = _filters(sql)
        if "name" in filters:
            table = f"{filters['database']}.{filters['name']}"
            return len(self.members) if table in self.definitions else 0
        return len(self.members)

    def query(self, sql: str, *, settings: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        self.queries.append(sql)
        self.query_settings.append(settings)
        rows = self._scope_metadata(sql)
        if rows is None:
            rows = self._table_metadata(sql)
        if rows is not None:
            return SimpleNamespace(result_rows=rows)
        if "startsWith" in sql and not self.recovery_artifacts:
            return SimpleNamespace(result_rows=[])
        if "system.processes" in sql or "system, processes" in sql:
            return SimpleNamespace(result_rows=[(0,)])
        if "system.parts" in sql or "system, parts" in sql:
            rows = (
                [(host, 1, 1, 1, 1) for host in self.members]
                if "hostName()" in sql
                else [(1, 2, 1, 1)]
            )
            return SimpleNamespace(result_rows=rows)
        if sql.startswith("SELECT count()"):
            return SimpleNamespace(result_rows=[(self._count(sql),)])
        return SimpleNamespace(result_rows=[("target__replace_abcd",)])

    def command(self, sql: str, **_kwargs: Any) -> None:
        self.commands.append(sql)
        special = exchange_or_rename(sql)
        if special is not None:
            operation, _clusters = special
            left, right = (_table_name(table) for table in operation.expressions)
            if operation.this == "EXCHANGE":
                self._exchange(left, right)
            else:
                self._rename(left, right)
            return
        statement = parse_one(sql, read=ClickHouseDialect)
        if isinstance(statement, exp.Create):
            self._create(statement)
        elif isinstance(statement, exp.Drop):
            for target in statement.args.get("tables") or [statement.this]:
                name = _table_name(target)
                self.definitions.pop(name, None)
                self.table_identities.pop(name, None)
                self.table_rows.pop(name, None)
        elif isinstance(statement, exp.Insert):
            target = (
                statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
            )
            _columns, rows = self._select_rows(statement.expression)
            self.table_rows[_table_name(target)].extend(rows)

    def _select_rows(self, select: exp.Select) -> tuple[list[str], list[tuple[Any, ...]]]:
        assert select.args.get("where") is None
        source = select.args["from_"].this
        if isinstance(source, exp.Subquery):
            columns, rows = self._select_rows(source.this)
        else:
            name = _table_name(source)
            rows = self.rows(name)
            if name in self.definitions:
                create = parse_one(self.definitions[name], read=ClickHouseDialect)
                columns = [column.name for column in create.this.expressions]
            else:
                columns = self.input_columns[name]
        records = [dict(zip(columns, row)) for row in rows]
        return _selected_rows(select, records, columns)

    def _create(self, create: exp.Create) -> None:
        table = create.this.this if isinstance(create.this, exp.Schema) else create.this
        name = _table_name(table)
        assert name not in self.definitions
        declared = create.find(exp.UuidProperty)
        self.definitions[name] = create.sql(dialect=ClickHouseDialect)
        self.table_identities[name] = UUID(declared.this.name) if declared else uuid4()
        self.table_rows[name] = []

    def _rename(self, source: str, target: str) -> None:
        assert target not in self.definitions
        create = parse_one(self.definitions.pop(source), read=ClickHouseDialect)
        self.definitions[target] = retarget_create(create, target, None).sql(
            dialect=ClickHouseDialect
        )
        self.table_identities[target] = self.table_identities.pop(source)
        self.table_rows[target] = self.table_rows.pop(source)

    def _exchange(self, left: str, right: str) -> None:
        left_create = parse_one(self.definitions[left], read=ClickHouseDialect)
        right_create = parse_one(self.definitions[right], read=ClickHouseDialect)
        self.definitions[left] = retarget_create(right_create, left, None).sql(
            dialect=ClickHouseDialect
        )
        self.definitions[right] = retarget_create(left_create, right, None).sql(
            dialect=ClickHouseDialect
        )
        for catalog in (self.table_identities, self.table_rows):
            catalog[left], catalog[right] = catalog[right], catalog[left]

    def rows(self, name: str) -> list[tuple[Any, ...]]:
        definition = self.definitions.get(name)
        if definition is not None:
            engine = parse_one(definition, read=ClickHouseDialect).find(exp.EngineProperty).this
            if engine.name.casefold() == "distributed":
                database, relation = engine.expressions[1:3]
                return self.rows(f"{database.name}.{relation.name}")
        return list(self.table_rows[name])


def _position(expression: exp.Expression, index: int) -> int:
    if isinstance(expression, exp.Alias):
        expression = expression.this
    return int(expression.this) if isinstance(expression, exp.Literal) else index


def _table_name(table: exp.Expr) -> str:
    function = table.this if isinstance(table, exp.Table) else table
    if isinstance(function, exp.Anonymous):
        _cluster, database, relation, _arguments = cluster_arguments(function, "sandbox")
        return f"{database}.{relation}"
    return ".".join(TableIdentifier.parse(table.sql(dialect=ClickHouseDialect), "ch").parts)

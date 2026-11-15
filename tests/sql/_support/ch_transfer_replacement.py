"""Stateful endpoint transports for public ClickHouse transfer replacement tests."""

from __future__ import annotations

import re
from copy import deepcopy
from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.outage_client import OutageClient
from analytics_toolkit.sql.backends.ch.outage_context import INTERNAL_IO
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import parse_create_table
from analytics_toolkit.sql.execution.observation import observed_connection
from sqlglot import exp, parse_one
from sqlglot.tokens import TokenType

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.reconfigure_conversion_scope import _name, _scope_sql
from tests.sql._support.reconfigure_pair_scope import PairScopeClient


class _Result:
    def __init__(self, rows, names=(), types=()):
        self.result_rows = list(rows)
        self.column_names = tuple(names)
        self.column_types = tuple(types or ["UInt64"] * len(names))
        self.result_columns = (
            list(zip(*self.result_rows)) if self.result_rows else [() for _name in names]
        )
        self.row_count = len(self.result_rows)


class ReplacementScopeState(PairScopeClient):
    """Keep the old B storage and an independently owned A companion distinct."""

    def __init__(self):
        super().__init__()
        self.clusters.update(
            incoming=(self.coordinator,),
            storage_alias=(self.remote,),
            coordinator_only=(self.coordinator,),
        )
        self.host_tables[self.remote].pop(self.source_table)
        self.host_tables[self.remote]["analytics.events_shard"].rows = [(11,), (12,), (13,)]
        physical = parse_create_table(
            "CREATE TABLE analytics.events_shard (id UInt64) ENGINE=MergeTree ORDER BY id",
            "analytics.events_shard",
        )
        self.foreign_rows = [(value,) for value in range(1001, 1101)]
        self._create(self.coordinator, physical, self.foreign_rows)
        self.created.clear()
        self.opened = []
        self.inserts = []
        self.metadata_transform = None
        self.metadata_faults = []

    def open(self, config, **_kwargs):
        matches = [
            host
            for host, (name, address, _port) in self.host_endpoints.items()
            if config.host in {host, name, address}
        ]
        assert len(matches) == 1, (config.host, matches)
        raw = _Connection(self, matches[0], native=config.driver == "native")
        self.opened.append(raw)
        if INTERNAL_IO.get():
            return raw
        wrapped = OutageClient(observed_connection(raw), config)
        self.wrappers.append(wrapped)
        return wrapped


class _Connection:
    server_tz = "UTC"
    effective_read_timezone = "UTC"
    effective_write_timezone = "UTC"

    def __init__(self, state, host, *, native):
        self.state = state
        self.host = host
        self.is_native_transport = native
        self.closed = False

    def close(self):
        self.closed = True

    def _reference(self, target):
        if isinstance(target, exp.Schema):
            target = target.this
        function = target.this if isinstance(target, exp.Table) else target
        if isinstance(function, exp.Anonymous):
            assert function.name.casefold() in {"cluster", "clusterallreplicas"}
            cluster, database, relation, _trailing = cluster_arguments(function, "analytics")
            return self.state.clusters[cluster], f"{database}.{relation}"
        assert isinstance(target, exp.Table), target
        return (self.host,), _name(target)

    def _metadata(self, name, hosts):
        if name == "system.processes":
            return []
        if name == "system.parts":
            return [
                {
                    **self.state._host_record(host),
                    "database": table.split(".", 1)[0],
                    "table": table.split(".", 1)[1],
                    "active": 1,
                    "rows": len(value.rows),
                    "max_block_number": len(value.rows),
                    "data_version": len(value.rows),
                }
                for host in hosts
                for table, value in self.state.host_tables[host].items()
                if value.route is None and value.rows
            ]
        records = self.state._metadata(name, hosts)
        if name == "system.clusters":
            records = [{**record, "internal_replication": 0} for record in records]
        if name == "system.columns":
            records = [
                {**record, "default_kind": "", "default_type": "", "default_expression": ""}
                for record in records
            ]
        return records

    def _source(self, select):
        source = select.args.get("from_")
        if source is None:
            return [
                {
                    **self.state._host_record(self.host),
                    "currentDatabase": "analytics",
                    "getMacro": "storage",
                    "timezone": "UTC",
                }
            ], []
        target = source.this
        if isinstance(target, exp.Subquery):
            result = self._select(target.this)
            return [dict(zip(result.column_names, row)) for row in result.result_rows], list(
                result.column_names
            )
        hosts, name = self._reference(target)
        if name.startswith("system."):
            return self._metadata(name, hosts), []
        rows = []
        columns = []
        for host in hosts:
            value = self.state.host_tables[host][name]
            columns = list(value.schema)
            rows.extend(dict(zip(columns, row)) for row in self.state._read(host, name))
        return rows, columns

    def _select(self, select):
        if isinstance(select, exp.Subquery):
            return self._select(select.this)
        assert isinstance(select, exp.Select), select
        records, source_names = self._source(select)
        condition = select.args.get("where")
        if condition is not None:
            records = [record for record in records if _value(condition.this, record)]
        order = select.args.get("order")
        if order is not None:
            records.sort(
                key=lambda record: tuple(_value(item.this, record) for item in order.expressions)
            )
        names, rows = _selected_rows(select, records, source_names)
        if select.args.get("distinct") is not None:
            rows = [row for index, row in enumerate(rows) if row not in rows[:index]]
        offset = select.args.get("offset")
        if offset is not None:
            rows = rows[int(offset.expression.this) :]
        limit = select.args.get("limit")
        if limit is not None:
            rows = rows[: int(limit.expression.this)]
        return _Result(rows, names)

    def query(self, statement, settings=None, **_kwargs):
        assert not self.closed
        self.state.queries.append(statement)
        self.state.query_settings.append(deepcopy(settings))
        body = strip_query_label(statement)
        if body.startswith("SHOW CREATE TABLE "):
            value = self.state.host_tables[self.host][_name(body[len("SHOW CREATE TABLE ") :])]
            return _Result([(value.create.sql(dialect=ClickHouseDialect),)], ["statement"])
        if body.startswith("EXISTS TABLE "):
            name = _name(body[len("EXISTS TABLE ") :])
            return _Result([(int(name in self.state.host_tables[self.host]),)], ["result"])
        tree = parse_one(body, read=ClickHouseDialect)
        if isinstance(tree, exp.Describe):
            hosts, name = self._reference(tree.this)
            value = self.state.host_tables[hosts[0]][name]
            return _Result(
                [(column, native, "") for column, native in value.schema.items()],
                ["name", "type", "default_type"],
                ["String", "String", "String"],
            )
        result = self._select(tree)
        if self.state.metadata_transform is not None:
            replacement = self.state.metadata_transform(tree, deepcopy(result.result_rows))
            if replacement is not None:
                self.state.metadata_faults.append((tree.copy(), result.column_names))
                return _Result(replacement, result.column_names, result.column_types)
        return result

    def _insert(self, target, rows, columns=None):
        rows = [tuple(row) for row in rows]
        hosts, name = self._reference(target)
        if isinstance(target, exp.Schema):
            columns = [item.name for item in target.expressions]
        host = hosts[0]
        value = self.state.host_tables[host][name]
        if columns is not None:
            assert set(columns) == set(value.schema)
            rows = [
                tuple(dict(zip(columns, row))[column] for column in value.schema) for row in rows
            ]
        self.state.insert_rows(host, name, rows)

    def insert(self, table, data, column_names, **kwargs):
        assert not self.closed
        rows = deepcopy(list(data))
        self.state.inserts.append(
            {
                "host": self.host,
                "table": table,
                "rows": rows,
                "columns": list(column_names),
                "options": deepcopy(kwargs),
            }
        )
        target = parse_one(f"SELECT * FROM {table}", read=ClickHouseDialect).args["from_"].this
        self._insert(target, rows, column_names)

    def create_insert_context(self, **kwargs):
        assert not self.closed
        return SimpleNamespace(**deepcopy(kwargs), original_table=kwargs["table"], data=None)

    def data_insert(self, context):
        assert not self.closed
        assert context.column_oriented is False
        columns = list(context.column_names)
        rows = [tuple(row) for row in deepcopy(list(context.data))]
        options = {
            key: deepcopy(getattr(context, key))
            for key in ("settings", "transport_settings", "column_types", "column_type_names")
        }
        self.state.inserts.append(
            {
                "host": self.host,
                "table": context.table,
                "original_table": context.original_table,
                "columns": columns,
                "rows": rows,
                "options": options,
            }
        )
        column_sql = ", ".join(
            exp.to_identifier(column, quoted=True).sql(dialect=ClickHouseDialect)
            for column in columns
        )
        command = _parse_command(f"INSERT INTO {context.table} ({column_sql}) SELECT 1")
        self._insert(command.this, rows, columns)

    def command(self, statement, settings=None):
        assert not self.closed
        self.state.commands.append(statement)
        self.state.command_settings.append(deepcopy(settings))
        cluster, body = _scope_sql(strip_query_label(statement))
        hosts = self.state.clusters[cluster] if cluster is not None else (self.host,)
        for host in hosts:
            self.state.host_commands[host].append(statement)
        operation = body.lstrip().split(None, 1)[0].upper()
        if operation in {"RENAME", "EXCHANGE"}:
            self.state._rename(body, hosts, exchange=operation == "EXCHANGE")
            return
        tree = _parse_command(body)
        if isinstance(tree, exp.Create):
            self._create_tables(tree, hosts)
        elif isinstance(tree, exp.Drop):
            self._drop_tables(tree, hosts)
        elif isinstance(tree, exp.Insert):
            self._insert(tree.this, self._select(tree.expression).result_rows)
        else:
            message = f"Unsupported replacement transport command: {statement}"
            raise TypeError(message)

    def _create_tables(self, tree, hosts):
        _validate_memory_storage(tree)
        name = _name(tree.this.this)
        for host in hosts:
            if name in self.state.host_tables[host] and tree.args.get("exists"):
                continue
            self.state._create(host, tree)

    def _drop_tables(self, tree, hosts):
        for target in tree.args.get("tables") or [tree.this]:
            name = _name(target)
            for host in hosts:
                assert tree.args.get("exists") or name in self.state.host_tables[host]
                self.state.host_tables[host].pop(name, None)


def _validate_memory_storage(tree):
    engine = tree.find(exp.EngineProperty)
    if engine is None or engine.this.name.casefold() != "memory":
        return
    properties = tree.args.get("properties")
    unsupported = (exp.Order, exp.PartitionedByProperty, exp.PrimaryKey, exp.SampleProperty)
    clauses = properties.expressions if properties is not None else ()
    if (
        any(isinstance(clause, unsupported) for clause in clauses)
        or tree.this.find(exp.PrimaryKeyColumnConstraint) is not None
    ):
        message = "Memory storage does not support sorting, partition, primary or sampling keys."
        raise ValueError(message)


def _closing_parenthesis(tokens, start):
    assert tokens[start].token_type == TokenType.L_PAREN
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].token_type == TokenType.L_PAREN:
            depth += 1
        elif tokens[index].token_type == TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                return index
    message = "Unterminated replacement transport parenthesis."
    raise AssertionError(message)


def _parse_command(statement):
    tokens = ClickHouseDialect().tokenize(statement)
    if [token.text.upper() for token in tokens[:3]] != ["INSERT", "INTO", "FUNCTION"]:
        return parse_one(statement, read=ClickHouseDialect)
    function_end = _closing_parenthesis(tokens, 4)
    function_sql = statement[tokens[3].start : tokens[function_end].end + 1]
    target = parse_one(f"SELECT * FROM {function_sql}", read=ClickHouseDialect).args["from_"].this
    index = function_end + 1
    if tokens[index].token_type == TokenType.L_PAREN:
        columns_end = _closing_parenthesis(tokens, index)
        columns_sql = statement[tokens[index].end + 1 : tokens[columns_end].start]
        columns = parse_one(f"SELECT {columns_sql}", read=ClickHouseDialect).expressions
        assert columns
        assert all(
            isinstance(column, exp.Column)
            and len(column.parts) == 1
            and isinstance(column.this, exp.Identifier)
            for column in columns
        )
        target = exp.Schema(this=target, expressions=[column.this.copy() for column in columns])
        index = columns_end + 1
    assert tokens[index].token_type == TokenType.SELECT
    source = parse_one(statement[tokens[index].start :], read=ClickHouseDialect)
    assert isinstance(source, exp.Select)
    return exp.Insert(this=target, expression=source)


def _selected_rows(select, records, source_names):
    expressions = select.expressions
    names = [item.alias_or_name or item.sql(dialect=ClickHouseDialect) for item in expressions]
    if any(isinstance(item, exp.Star) for item in expressions):
        return source_names, [tuple(record[name] for name in source_names) for record in records]
    if not any(item.find(exp.AggFunc) is not None for item in expressions):
        return names, [tuple(_value(item, record) for item in expressions) for record in records]
    group = select.args.get("group")
    groups = {(): records} if group is None else {}
    for record in records if group is not None else []:
        key = tuple(_value(item, record) for item in group.expressions)
        groups.setdefault(key, []).append(record)
    return names, [
        tuple(_aggregate(item, group_rows) for item in expressions)
        for group_rows in groups.values()
    ]


def _aggregate(expression, records):
    if isinstance(expression, exp.Alias):
        return _aggregate(expression.this, records)
    if isinstance(expression, exp.Count):
        return len(records)
    if isinstance(expression, exp.Sum):
        return sum(_value(expression.this, record) for record in records)
    if isinstance(expression, exp.Max):
        return max((_value(expression.this, record) for record in records), default=0)
    return _value(expression, records[0])


_UNKNOWN = object()


def _predicate(expression, record):
    if isinstance(expression, exp.And):
        return _value(expression.this, record) and _value(expression.expression, record)
    if isinstance(expression, exp.Or):
        return _value(expression.this, record) or _value(expression.expression, record)
    if isinstance(expression, exp.EQ):
        return _value(expression.this, record) == _value(expression.expression, record)
    if isinstance(expression, exp.In):
        return _value(expression.this, record) in [
            _value(item, record) for item in expression.expressions
        ]
    return _text_predicate(expression, record)


def _text_predicate(expression, record):
    if isinstance(expression, exp.Like):
        pattern = (
            re.escape(str(_value(expression.expression, record)))
            .replace("%", ".*")
            .replace("_", ".")
        )
        return re.fullmatch(pattern, str(_value(expression.this, record))) is not None
    if isinstance(expression, exp.StartsWith):
        return str(_value(expression.this, record)).startswith(
            str(_value(expression.expression, record))
        )
    if isinstance(expression, exp.Anonymous):
        name = expression.name.casefold()
        if name == "startswith":
            left, right = expression.expressions
            return str(_value(left, record)).startswith(str(_value(right, record)))
    return _UNKNOWN


def _value(expression, record):
    if isinstance(expression, (exp.Alias, exp.Paren, exp.Cast)):
        return _value(expression.this, record)
    scalar = _literal_value(expression)
    if scalar is not _UNKNOWN:
        return scalar
    predicate = _predicate(expression, record)
    if predicate is not _UNKNOWN:
        return predicate
    if isinstance(expression, exp.Anonymous) and expression.name.casefold() == "tostring":
        return str(_value(expression.expressions[0], record))
    return _record_value(expression, record)


def _literal_value(expression):
    if isinstance(expression, exp.Literal):
        return expression.to_py()
    if isinstance(expression, exp.Boolean):
        return expression.this
    if isinstance(expression, exp.Null):
        return None
    return _UNKNOWN


def _record_value(expression, record):
    name = expression.name
    if not name and isinstance(expression, exp.Func):
        name = expression.sql_name().replace("_", "")
    for key, value in record.items():
        if key.casefold() == name.casefold():
            return value
    message = f"Unsupported replacement transport expression: {expression}"
    raise AssertionError(message)

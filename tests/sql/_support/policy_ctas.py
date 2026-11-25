"""Table-consistent metadata for the ClickHouse CTAS policy facade tests."""

from __future__ import annotations

from types import SimpleNamespace

from analytics_toolkit.sql.backends.ch.cluster_functions import cluster_arguments
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.reconfigure_metadata import ReconfigureMetadata


class _PolicyMetadata(ReconfigureMetadata):
    @property
    def clusters(self):
        # These are four names for the same verified coordinator instance.
        # The public local replacement case therefore keeps actual placement.
        return dict.fromkeys(("core", "storage", "facades", "routing"), self.client.source_hosts)


class Client:
    def __init__(self, *, exists=False, close_error=False):
        self.exists = exists
        self.close_error = close_error
        self.commands = []
        self.queries = []
        self.query_settings = []
        self.close_calls = 0
        self.ddl = "CREATE TABLE analytics.events (id Int64) ENGINE = MergeTree ORDER BY id"
        self.source_hosts = (("coordinator", "127.0.0.1", 9000),)
        self.metadata_macros = {"cluster": "storage"}
        self.metadata = _PolicyMetadata(self)

    def metadata_definitions(self):
        return {("analytics", "events"): self.ddl} if self.exists else {}

    def _table(self, sql, prefix):
        return parse_one(sql[len(prefix) :], read=ClickHouseDialect, into=exp.Table)

    def _state(self, table):
        self.metadata._seed()
        host, address, port = self.source_hosts[0]
        identity = table.db or "analytics", table.name
        return self.metadata.tables.get(((address or host, port), identity))

    def _value_type(self, value, source_schema):
        if isinstance(value, exp.Column):
            return source_schema[value.name]
        if isinstance(value, exp.Cast):
            return value.args["to"].sql(dialect=ClickHouseDialect)
        assert isinstance(value, exp.Literal)
        if value.is_string:
            return "String"
        # This fixture's source SDK declares its integer projection as Int64.
        return "Int64" if value.is_int else "Float64"

    def _describe(self, expression):
        if isinstance(expression, (exp.Subquery, exp.Paren)):
            return self._describe(expression.this)
        if isinstance(expression, exp.Table):
            state = self._state(expression)
            assert state is not None
            return [
                (column.name, column.args["kind"].sql(dialect=ClickHouseDialect))
                for column in state.this.expressions
                if isinstance(column, exp.ColumnDef)
            ]
        assert isinstance(expression, exp.Select)
        source = expression.args.get("from_")
        source_columns = self._describe(source.this) if source is not None else []
        source_schema = dict(source_columns)
        columns = []
        for projection in expression.expressions:
            if isinstance(projection, exp.Star):
                columns.extend(source_columns)
                continue
            value = projection.this if isinstance(projection, exp.Alias) else projection
            columns.append((projection.alias_or_name, self._value_type(value, source_schema)))
        return columns

    def _metadata_rows(self, statement):
        select = parse_one(statement, read=ClickHouseDialect)
        if not isinstance(select, exp.Select):
            return None
        source = select.args.get("from_")
        if source is None:
            names = {
                item.name.casefold()
                if isinstance(item, exp.Anonymous)
                else item.sql_name().replace("_", "").casefold()
                for item in select.expressions
                if isinstance(item, exp.Func)
            }
            if not names or not names <= {"serveruuid", "tcpport", "getmacro"}:
                return None
        else:
            table = source.this
            if not isinstance(table, exp.Table):
                return None
            if isinstance(table.this, exp.Anonymous):
                if table.this.name.casefold() not in {"cluster", "clusterallreplicas"}:
                    return None
                _cluster, database, _relation, _trailing = cluster_arguments(
                    table.this, "analytics"
                )
            else:
                database = table.db
            if database != "system":
                return None
        return self.metadata.rows(statement)

    def query(self, sql, settings=None):
        self.queries.append(sql)
        self.query_settings.append(settings)
        statement = strip_query_label(sql)
        if statement.startswith("DESCRIBE TABLE "):
            source = parse_one(statement[len("DESCRIBE TABLE ") :], read=ClickHouseDialect)
            if not isinstance(source, (exp.Select, exp.Subquery, exp.Paren)):
                source = self._table(statement, "DESCRIBE TABLE ")
            rows = self._describe(source)
        elif statement.startswith("EXISTS TABLE "):
            rows = [(int(self._state(self._table(statement, "EXISTS TABLE ")) is not None),)]
        elif statement.startswith("SHOW CREATE TABLE "):
            self.metadata._seed()
            rows = [(self.metadata.show_create(statement),)]
        else:
            rows = self._metadata_rows(statement)
            if rows is None:
                rows = []
        return SimpleNamespace(result_rows=rows, column_names=["id"], column_types=["Int64"])

    def command(self, sql, **_kwargs):
        self.commands.append(sql)
        statement = strip_query_label(sql)
        create = parse_one(statement, read=ClickHouseDialect)
        if isinstance(create, exp.Create):
            if not isinstance(create.this, exp.Schema):
                columns = self._describe(create.expression)
                create.set(
                    "this",
                    exp.Schema(
                        this=create.this,
                        expressions=[
                            exp.ColumnDef(
                                this=exp.to_identifier(name),
                                kind=exp.DataType.build(native, dialect="clickhouse"),
                            )
                            for name, native in columns
                        ],
                    ),
                )
            create.set("expression", None)
            statement = create.sql(dialect=ClickHouseDialect)
        self.metadata.command(statement)
        self.exists = self._state(exp.to_table("analytics.events")) is not None

    def close(self):
        self.close_calls += 1
        if self.close_error:
            message = "close failed"
            raise ConnectionError(message)

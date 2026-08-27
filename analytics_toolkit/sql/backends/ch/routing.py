from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from analytics_toolkit.sql.backends.transfer_stage import is_transfer_stage_identifier
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)

DEFAULT_CLUSTER_SHARDING_KEY = "rand()"
ON_CLUSTER_COMMAND_SETTINGS = {
    "distributed_ddl_task_timeout": 0,
    "distributed_ddl_output_mode": "none",
}
_LOCAL_SQL_EXECUTION: ContextVar[bool] = ContextVar(
    "analytics_toolkit_clickhouse_local_sql_execution",
    default=False,
)
_ON_CLUSTER_ARG_STATEMENTS = (
    exp.Alter,
    exp.Delete,
    exp.Detach,
    exp.Drop,
    exp.TruncateTable,
)
_PLAN_PLACEHOLDER_RE = re.compile(
    r"<(?:source|stage|runtime|lazy|dynamically|shared|writer|external|\d+\s+dataframe)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChClusterRouting:
    cluster: str
    sharding_key: str = DEFAULT_CLUSTER_SHARDING_KEY


def parse_cluster_routing(raw: Any, connection_key: str) -> ChClusterRouting | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        message = (
            f"SQL connection '{connection_key}' field 'cluster_routing' must be a JSON object."
        )
        raise SqlConfigError(message)
    unexpected = set(raw) - {"cluster", "sharding_key"}
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        message = (
            f"SQL connection '{connection_key}' field 'cluster_routing' has unsupported "
            f"field(s): {names}."
        )
        raise SqlConfigError(message)
    cluster = _required_config_string(raw, connection_key, "cluster")
    sharding_key = (
        _required_config_string(raw, connection_key, "sharding_key")
        if "sharding_key" in raw
        else DEFAULT_CLUSTER_SHARDING_KEY
    )
    _validate_sharding_key(sharding_key, connection_key)
    return ChClusterRouting(cluster=cluster, sharding_key=sharding_key)


def prepare_sql(adapter: Any, config: Any, sql: str) -> str:
    del adapter
    if _LOCAL_SQL_EXECUTION.get():
        return sql
    routing = getattr(config, "cluster_routing", None)
    if routing is None:
        return sql
    return route_sql(sql, routing=routing, database=getattr(config, "database", None))


def route_sql(
    sql: str,
    *,
    routing: ChClusterRouting,
    database: str | None,
    cluster_override: str | None = None,
) -> str:
    try:
        parsed = sqlglot.parse(sql, read="clickhouse", error_level=ErrorLevel.RAISE)
    except SqlglotError as exc:
        message = "ClickHouse cluster routing could not parse the SQL safely."
        raise InvalidSqlInputError(message) from exc
    statements = cast(
        "list[exp.Expression]",
        [statement for statement in parsed if statement is not None],
    )
    if len(statements) != len(parsed):
        message = "ClickHouse cluster routing could not parse the SQL safely."
        raise InvalidSqlInputError(message)
    if not statements:
        message = "ClickHouse cluster routing received empty SQL."
        raise InvalidSqlInputError(message)

    routed: list[str] = []
    for statement in statements:
        if isinstance(statement, exp.Command):
            message = "ClickHouse cluster routing does not support this statement syntax safely."
            raise InvalidSqlInputError(message)
        explicit_cluster = _explicit_cluster(statement)
        effective_cluster = explicit_cluster or cluster_override or routing.cluster
        _rewrite_query_sources(statement, effective_cluster, database)
        _route_statement_target(
            statement,
            effective_cluster,
            database,
            routing.sharding_key,
            has_explicit_cluster=explicit_cluster is not None,
        )
        _preserve_clickhouse_rand(statement)
        routed.append(statement.sql(dialect="clickhouse"))
    return ";\n".join(routed)


def wrap_client(client: Any, config: Any) -> Any:
    routing = getattr(config, "cluster_routing", None)
    if routing is None:
        return client
    return ClusterRoutingClient(
        client,
        routing=routing,
        database=getattr(config, "database", None),
    )


@contextmanager
def local_sql(connection: Any | None = None) -> Iterator[None]:
    del connection
    token = _LOCAL_SQL_EXECUTION.set(True)
    try:
        yield
    finally:
        _LOCAL_SQL_EXECUTION.reset(token)


def query_local(connection: Any, sql: str, **kwargs: Any) -> Any:
    with local_sql(connection):
        return connection.query(sql, **kwargs)


def command_local(connection: Any, sql: str, **kwargs: Any) -> Any:
    with local_sql(connection):
        return connection.command(sql, **kwargs)


def routed_connection_sql(connection: Any, sql: str) -> str:
    if not isinstance(connection, ClusterRoutingClient):
        return sql
    return connection.route(sql)


def connection_cluster_routing(connection: Any) -> ChClusterRouting | None:
    if not isinstance(connection, ClusterRoutingClient):
        return None
    return connection.cluster_routing


def prepare_plan_sql(
    adapter: Any,
    connection_key: str,
    sql: str,
    previous_sqls: Sequence[str],
) -> str:
    from analytics_toolkit.sql.connection.config import get_connection_config  # noqa: PLC0415

    try:
        config = get_connection_config(connection_key)
    except UnsupportedConnectionTypeError:
        return sql
    routing = getattr(config, "cluster_routing", None)
    if (
        routing is None
        or _PLAN_PLACEHOLDER_RE.search(sql)
        or _is_local_create_fallback(sql, previous_sqls)
    ):
        return sql
    return prepare_sql(adapter, config, sql)


def execute_commands(adapter: Any, connection: Any, sqls: list[str]) -> None:
    clustered_create_targets: set[str] = set()
    for sql in sqls:
        create_target, has_cluster = _create_target(sql)
        if create_target is not None and has_cluster:
            clustered_create_targets.add(create_target)
        if (
            create_target is not None
            and not has_cluster
            and create_target in clustered_create_targets
        ):
            with local_sql(connection):
                adapter.execute_command(connection, sql)
        else:
            adapter.execute_command(connection, sql)


def _is_local_create_fallback(sql: str, previous_sqls: Sequence[str]) -> bool:
    create_target, has_cluster = _create_target(sql)
    if create_target is None or has_cluster or not previous_sqls:
        return False
    previous_target, previous_has_cluster = _create_target(previous_sqls[-1])
    return previous_has_cluster and previous_target == create_target


class ClusterRoutingClient:
    _client: Any
    _routing: ChClusterRouting
    _database: str | None

    def __init__(
        self,
        client: Any,
        *,
        routing: ChClusterRouting,
        database: str | None,
    ) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_routing", routing)
        object.__setattr__(self, "_database", database)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._client, name, value)

    @property
    def is_native_transport(self) -> bool:
        return bool(getattr(self._client, "is_native_transport", False))

    @property
    def cluster_routing(self) -> ChClusterRouting:
        return self._routing

    def command(self, sql: str, **kwargs: Any) -> Any:
        routed_sql = self.route(sql)
        if "ON CLUSTER" in routed_sql.upper():
            settings = dict(ON_CLUSTER_COMMAND_SETTINGS)
            settings.update(kwargs.get("settings") or {})
            kwargs["settings"] = settings
        try:
            return self._client.command(routed_sql, **kwargs)
        except TypeError:
            kwargs.pop("settings", None)
            return self._client.command(routed_sql, **kwargs)

    def query(self, sql: str, **kwargs: Any) -> Any:
        return self._client.query(self.route(sql), **kwargs)

    def query_df(self, sql: str, **kwargs: Any) -> Any:
        return self._client.query_df(self.route(sql), **kwargs)

    def query_df_stream(self, sql: str, **kwargs: Any) -> Any:
        return self._client.query_df_stream(self.route(sql), **kwargs)

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        **kwargs: Any,
    ) -> Any:
        if _LOCAL_SQL_EXECUTION.get():
            return self._client.insert(
                table=table,
                data=data,
                column_names=column_names,
                **kwargs,
            )
        target = self._insert_target(table)
        if self.is_native_transport:
            return self._client.insert(
                table=target,
                data=data,
                column_names=column_names,
                **kwargs,
            )
        return self._http_insert(
            table,
            target,
            data,
            column_names=column_names,
            column_oriented=False,
            **kwargs,
        )

    def insert_df(
        self,
        table: str,
        df: Any,
        column_names: Sequence[str],
        **kwargs: Any,
    ) -> Any:
        if _LOCAL_SQL_EXECUTION.get():
            return self._client.insert_df(
                table=table,
                df=df,
                column_names=column_names,
                **kwargs,
            )
        target = self._insert_target(table)
        if self.is_native_transport:
            return self._client.insert_df(target, df, column_names, **kwargs)
        return self._http_insert(
            table,
            target,
            df,
            column_names=column_names,
            column_oriented=True,
            **kwargs,
        )

    def close(self) -> None:
        self._client.close()

    def route(self, sql: str) -> str:
        if _LOCAL_SQL_EXECUTION.get():
            return sql
        return route_sql(sql, routing=self._routing, database=self._database)

    def _insert_target(self, table_name: str) -> str:
        table = _parse_table_name(table_name)
        database, relation = _table_parts(table, self._database)
        function = _cluster_function_sql(
            self._routing.cluster,
            database,
            relation,
            sharding_key=self._routing.sharding_key,
        )
        return f"FUNCTION {function}"

    def _http_insert(
        self,
        original_table: str,
        target: str,
        data: Any,
        *,
        column_names: Sequence[str],
        column_oriented: bool,
        **kwargs: Any,
    ) -> Any:
        context = self._client.create_insert_context(
            table=original_table,
            column_names=column_names,
            column_types=kwargs.pop("column_types", None),
            column_type_names=kwargs.pop("column_type_names", None),
            column_oriented=column_oriented,
            settings=kwargs.pop("settings", None),
            transport_settings=kwargs.pop("transport_settings", None),
        )
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            message = f"Unsupported routed ClickHouse insert option(s): {unexpected}."
            raise TypeError(message)
        context.table = target
        context.data = data
        return self._client.data_insert(context)


def _required_config_string(
    raw: Mapping[str, Any],
    connection_key: str,
    field_name: str,
) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value.strip():
        message = (
            f"SQL connection '{connection_key}' field "
            f"'cluster_routing.{field_name}' must be a non-empty string."
        )
        raise SqlConfigError(message)
    return value.strip()


def _validate_sharding_key(value: str, connection_key: str) -> None:
    try:
        parsed = sqlglot.parse(value, read="clickhouse", error_level=ErrorLevel.RAISE)
    except SqlglotError as exc:
        message = (
            f"SQL connection '{connection_key}' field "
            "'cluster_routing.sharding_key' must be a valid ClickHouse expression."
        )
        raise SqlConfigError(message) from exc
    if len(parsed) != 1 or isinstance(parsed[0], (exp.Command, exp.Query)):
        message = (
            f"SQL connection '{connection_key}' field "
            "'cluster_routing.sharding_key' must be a single ClickHouse expression."
        )
        raise SqlConfigError(message)


def _explicit_cluster(statement: exp.Expression) -> str | None:
    values = {
        _identifier_value(on_cluster.this) for on_cluster in statement.find_all(exp.OnCluster)
    }
    values.discard(None)
    if len(values) > 1:
        message = "ClickHouse cluster routing found conflicting ON CLUSTER values in one statement."
        raise InvalidSqlInputError(message)
    return next(iter(values), None)


def _rewrite_query_sources(
    statement: exp.Expression,
    cluster: str,
    database: str | None,
) -> None:
    for scope in traverse_scope(statement) or []:
        for table in list(scope.tables):
            if not isinstance(table.this, exp.Identifier):
                continue
            if table.args.get("db") is None and table.name in scope.cte_sources:
                continue
            _replace_source_table(table, cluster, database)


def _replace_source_table(
    table: exp.Table,
    cluster: str,
    default_database: str | None,
) -> None:
    database, relation = _table_parts(table, default_database)
    function_name = "clusterAllReplicas" if is_transfer_stage_identifier(relation) else "cluster"
    replacement = _cluster_table(
        cluster,
        database,
        relation,
        function_name=function_name,
    )
    for key, value in table.args.items():
        if key not in {"this", "db", "catalog"}:
            replacement.set(key, value.copy() if isinstance(value, exp.Expression) else value)
    replacement.add_comments(table.comments)
    table.replace(replacement)


def _route_statement_target(
    statement: exp.Expression,
    cluster: str,
    database: str | None,
    sharding_key: str,
    *,
    has_explicit_cluster: bool,
) -> None:
    if isinstance(statement, exp.Describe):
        target = statement.this
        if isinstance(target, exp.Table) and isinstance(target.this, exp.Identifier):
            statement.set(
                "this",
                _routed_table_from_table(
                    target,
                    cluster,
                    database,
                    sharding_key=sharding_key,
                ),
            )
        elif isinstance(target, exp.Subquery):
            _rewrite_query_sources(target.this, cluster, database)
        return
    if isinstance(statement, exp.Insert):
        target = statement.this
        if isinstance(target, exp.Table) and isinstance(target.this, exp.Identifier):
            routed_target = _routed_table_from_table(
                target,
                cluster,
                database,
                sharding_key=sharding_key,
            )
            statement.set("this", routed_target)
            statement.set(arg_key="is_function", value=True)
        return
    if has_explicit_cluster:
        return
    if isinstance(statement, exp.Create):
        if _is_temporary_create(statement):
            message = "ClickHouse cluster routing cannot safely route temporary tables."
            raise InvalidSqlInputError(message)
        _add_create_on_cluster(statement, cluster)
        return
    if isinstance(statement, _ON_CLUSTER_ARG_STATEMENTS):
        statement.set("cluster", exp.OnCluster(this=exp.Literal.string(cluster)))


def _routed_table_from_table(
    table: exp.Table,
    cluster: str,
    default_database: str | None,
    *,
    sharding_key: str,
) -> exp.Table:
    database, relation = _table_parts(table, default_database)
    return _cluster_table(
        cluster,
        database,
        relation,
        sharding_key=sharding_key,
    )


def _table_parts(table: exp.Table, default_database: str | None) -> tuple[str, str]:
    if table.args.get("catalog") is not None:
        message = "ClickHouse cluster routing does not support catalog-qualified tables."
        raise InvalidSqlInputError(message)
    database = _identifier_value(table.args.get("db")) or default_database
    if database is None:
        message = (
            "ClickHouse cluster routing requires .connections.database for "
            f"unqualified table {table.name!r}."
        )
        raise InvalidSqlInputError(message)
    return database, table.name


def _cluster_table(
    cluster: str,
    database: str,
    table: str,
    *,
    sharding_key: str | None = None,
    function_name: str = "cluster",
) -> exp.Table:
    arguments: list[exp.Expression] = [
        exp.Literal.string(cluster),
        exp.Literal.string(database),
        exp.Literal.string(table),
    ]
    if sharding_key is not None:
        arguments.append(exp.Var(this=sharding_key))
    return exp.Table(this=exp.Anonymous(this=function_name, expressions=arguments))


def _cluster_function_sql(
    cluster: str,
    database: str,
    table: str,
    *,
    sharding_key: str,
) -> str:
    return _cluster_table(
        cluster,
        database,
        table,
        sharding_key=sharding_key,
    ).sql(dialect="clickhouse")


def _add_create_on_cluster(statement: exp.Create, cluster: str) -> None:
    properties = statement.args.get("properties")
    if properties is None:
        properties = exp.Properties(expressions=[])
        statement.set("properties", properties)
    properties.append("expressions", exp.OnCluster(this=exp.Literal.string(cluster)))


def _is_temporary_create(statement: exp.Create) -> bool:
    properties = statement.args.get("properties")
    return bool(properties and properties.find(exp.TemporaryProperty))


def _preserve_clickhouse_rand(statement: exp.Expression) -> None:
    for function in list(statement.find_all(exp.Rand)):
        if str(function.meta.get("name", "")).lower() != "rand":
            continue
        replacement = exp.Anonymous(
            this="rand",
            expressions=[argument.copy() for argument in function.iter_expressions()],
        )
        replacement.add_comments(function.comments)
        function.replace(replacement)


def _identifier_value(value: Any) -> str | None:
    if isinstance(value, (exp.Identifier, exp.Literal)):
        normalized = str(value.this).strip()
        return normalized or None
    return None


def _create_target(sql: str) -> tuple[str | None, bool]:
    try:
        statement = sqlglot.parse_one(sql, read="clickhouse", error_level=ErrorLevel.RAISE)
    except SqlglotError:
        return None, False
    if not isinstance(statement, exp.Create):
        return None, False
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    if not isinstance(target, exp.Table):
        return None, False
    return target.sql(dialect="clickhouse"), statement.find(exp.OnCluster) is not None


def _parse_table_name(table_name: str) -> exp.Table:
    try:
        table = sqlglot.parse_one(
            table_name,
            read="clickhouse",
            into=exp.Table,
            error_level=ErrorLevel.RAISE,
        )
    except SqlglotError as exc:
        message = f"Invalid ClickHouse table name: {table_name!r}."
        raise InvalidSqlInputError(message) from exc
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        message = f"Invalid ClickHouse table name: {table_name!r}."
        raise InvalidSqlInputError(message)
    return table

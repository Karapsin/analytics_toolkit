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

from .managed_routing import ManagedPairResolver, ManagedTableRoute

DEFAULT_CLUSTER_SHARDING_KEY = "rand()"
MIN_CLUSTER_TABLE_FUNCTION_ARGS = 3
SHARDING_KEY_CLUSTER_TABLE_FUNCTION_ARGS = 4
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


@dataclass(frozen=True)
class _BinaryInsertRoute:
    context_table: str
    target_table: str
    foreground: bool = False


@dataclass(frozen=True)
class _StatementRoute:
    cluster: str
    database: str | None
    sharding_key: str
    managed_pair_resolver: ManagedPairResolver | None


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
    route_sql(sql, routing=routing, database=getattr(config, "database", None))
    return sql


def route_sql(
    sql: str,
    *,
    routing: ChClusterRouting,
    database: str | None,
    cluster_override: str | None = None,
    managed_pair_resolver: ManagedPairResolver | None = None,
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
        if managed_pair_resolver is not None and _changes_table_metadata(statement):
            managed_pair_resolver.invalidate()
        explicit_cluster = _explicit_cluster(statement)
        effective_cluster = explicit_cluster or cluster_override or routing.cluster
        _rewrite_query_sources(
            statement,
            effective_cluster,
            database,
            managed_pair_resolver=managed_pair_resolver,
        )
        _route_statement_target(
            statement,
            _StatementRoute(
                cluster=effective_cluster,
                database=database,
                sharding_key=routing.sharding_key,
                managed_pair_resolver=managed_pair_resolver,
            ),
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
    del adapter
    return route_sql(sql, routing=routing, database=getattr(config, "database", None))


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
    _managed_pairs: ManagedPairResolver

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
        object.__setattr__(self, "_managed_pairs", ManagedPairResolver(client))

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
        routed_upper = routed_sql.upper()
        if "ON CLUSTER" in routed_upper or routed_upper.lstrip().startswith("INSERT "):
            settings = dict(ON_CLUSTER_COMMAND_SETTINGS) if "ON CLUSTER" in routed_upper else {}
            if routed_upper.lstrip().startswith("INSERT "):
                settings["distributed_foreground_insert"] = 1
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
        insert_route = self._insert_route(table)
        if insert_route.foreground:
            settings = {"distributed_foreground_insert": 1}
            settings.update(kwargs.get("settings") or {})
            kwargs["settings"] = settings
        if self.is_native_transport:
            return self._client.insert(
                table=insert_route.target_table,
                data=data,
                column_names=column_names,
                **kwargs,
            )
        return self._http_insert(
            insert_route.context_table,
            insert_route.target_table,
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
        insert_route = self._insert_route(table)
        if insert_route.foreground:
            settings = {"distributed_foreground_insert": 1}
            settings.update(kwargs.get("settings") or {})
            kwargs["settings"] = settings
        if self.is_native_transport:
            return self._client.insert_df(insert_route.target_table, df, column_names, **kwargs)
        return self._http_insert(
            insert_route.context_table,
            insert_route.target_table,
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
        return route_sql(
            sql,
            routing=self._routing,
            database=self._database,
            managed_pair_resolver=self._managed_pairs,
        )

    def _insert_route(self, table_name: str) -> _BinaryInsertRoute:
        table = _parse_table_name(table_name)
        database, relation = _table_parts(table, self._database)
        managed_route = _resolve_managed_route(
            self._managed_pairs,
            cluster=self._routing.cluster,
            database=database,
            relation=relation,
        )
        if managed_route is not None and managed_route.mode == "local":
            return _BinaryInsertRoute(table_name, table_name, foreground=True)
        routed_database = managed_route.database if managed_route is not None else database
        routed_relation = managed_route.table if managed_route is not None else relation
        context_table = (
            _table_name_with_route(table, managed_route)
            if managed_route is not None
            else table_name
        )
        function = _cluster_function_sql(
            self._routing.cluster,
            routed_database,
            routed_relation,
            sharding_key=self._routing.sharding_key,
            function_name=_route_function_name(
                managed_route,
                routed_relation,
                routed_database,
            ),
        )
        return _BinaryInsertRoute(context_table, f"FUNCTION {function}")

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
    *,
    managed_pair_resolver: ManagedPairResolver | None = None,
) -> None:
    _normalize_explicit_cluster_tables(
        statement,
        cluster,
        database,
        managed_pair_resolver=managed_pair_resolver,
    )
    for scope in traverse_scope(statement) or []:
        for table in list(scope.tables):
            if not isinstance(table.this, exp.Identifier):
                continue
            if table.args.get("db") is None and table.name in scope.cte_sources:
                continue
            _replace_source_table(
                table,
                cluster,
                database,
                managed_pair_resolver=managed_pair_resolver,
            )


def _normalize_explicit_cluster_tables(
    statement: exp.Expression,
    cluster: str,
    default_database: str | None,
    *,
    managed_pair_resolver: ManagedPairResolver | None,
) -> None:
    if managed_pair_resolver is None:
        return
    for table in list(statement.find_all(exp.Table)):
        function = table.this
        if not isinstance(function, exp.Anonymous) or str(function.this).lower() not in {
            "cluster",
            "clusterallreplicas",
        }:
            continue
        arguments = list(function.expressions)
        if len(arguments) < MIN_CLUSTER_TABLE_FUNCTION_ARGS:
            continue
        database = _identifier_value(arguments[1]) or default_database
        relation = _identifier_value(arguments[2])
        if database is None or relation is None:
            continue
        managed_route = _resolve_managed_route(
            managed_pair_resolver,
            cluster=cluster,
            database=database,
            relation=relation,
        )
        if managed_route is None:
            continue
        if managed_route.mode == "local":
            replacement = exp.Table(
                this=exp.to_identifier(relation),
                db=exp.to_identifier(database),
            )
        else:
            sharding_key = (
                arguments[3].sql(dialect="clickhouse")
                if len(arguments) >= SHARDING_KEY_CLUSTER_TABLE_FUNCTION_ARGS
                else None
            )
            replacement = _cluster_table(
                cluster,
                managed_route.database,
                managed_route.table,
                sharding_key=sharding_key,
                function_name=_route_function_name(
                    managed_route,
                    managed_route.table,
                    managed_route.database,
                ),
            )
        _copy_table_decorations(table, replacement)
        table.replace(replacement)


def _replace_source_table(
    table: exp.Table,
    cluster: str,
    default_database: str | None,
    *,
    managed_pair_resolver: ManagedPairResolver | None,
) -> None:
    database, relation = _table_parts(table, default_database)
    managed_route = _resolve_managed_route(
        managed_pair_resolver,
        cluster=cluster,
        database=database,
        relation=relation,
    )
    if managed_route is not None and managed_route.mode == "local":
        return
    if managed_route is not None:
        database = managed_route.database
        relation = managed_route.table
    function_name = _route_function_name(managed_route, relation, database)
    replacement = _cluster_table(
        cluster,
        database,
        relation,
        function_name=function_name,
    )
    _copy_table_decorations(table, replacement)
    table.replace(replacement)


def _copy_table_decorations(source: exp.Table, target: exp.Table) -> None:
    for key, value in source.args.items():
        if key not in {"this", "db", "catalog"}:
            target.set(key, value.copy() if isinstance(value, exp.Expression) else value)
    target.add_comments(source.comments)


def _route_statement_target(
    statement: exp.Expression,
    route: _StatementRoute,
    *,
    has_explicit_cluster: bool,
) -> None:
    if isinstance(statement, exp.Describe):
        target = statement.this
        if isinstance(target, exp.Table) and isinstance(target.this, exp.Identifier):
            routed_target, _ = _routed_table_from_table(target, route)
            statement.set(
                "this",
                routed_target,
            )
        elif isinstance(target, exp.Subquery):
            _rewrite_query_sources(
                target.this,
                route.cluster,
                route.database,
                managed_pair_resolver=route.managed_pair_resolver,
            )
        return
    if isinstance(statement, exp.Insert):
        _route_insert_target(statement, route)
        return
    if has_explicit_cluster:
        return
    if isinstance(statement, exp.Create):
        if _is_temporary_create(statement):
            message = "ClickHouse cluster routing cannot safely route temporary tables."
            raise InvalidSqlInputError(message)
        _add_create_on_cluster(statement, route.cluster)
        return
    if isinstance(statement, _ON_CLUSTER_ARG_STATEMENTS):
        statement.set("cluster", exp.OnCluster(this=exp.Literal.string(route.cluster)))


def _route_insert_target(statement: exp.Insert, route: _StatementRoute) -> None:
    target = statement.this
    target_table = target.this if isinstance(target, exp.Schema) else target
    if not isinstance(target_table, exp.Table) or not isinstance(
        target_table.this,
        exp.Identifier,
    ):
        return
    routed_target, is_function = _routed_table_from_table(target_table, route)
    if isinstance(target, exp.Schema):
        routed_schema = target.copy()
        routed_schema.set("this", routed_target)
        routed_target = routed_schema
    statement.set("this", routed_target)
    statement.set(arg_key="is_function", value=is_function)


def _routed_table_from_table(
    table: exp.Table,
    route: _StatementRoute,
) -> tuple[exp.Expression, bool]:
    database, relation = _table_parts(table, route.database)
    managed_route = _resolve_managed_route(
        route.managed_pair_resolver,
        cluster=route.cluster,
        database=database,
        relation=relation,
    )
    if managed_route is not None and managed_route.mode == "local":
        return table.copy(), False
    if managed_route is not None:
        database = managed_route.database
        relation = managed_route.table
    return (
        _cluster_table(
            route.cluster,
            database,
            relation,
            sharding_key=route.sharding_key,
            function_name=_route_function_name(managed_route, relation, database),
        ),
        True,
    )


def _resolve_managed_route(
    resolver: ManagedPairResolver | None,
    *,
    cluster: str,
    database: str,
    relation: str,
) -> ManagedTableRoute | None:
    if resolver is None:
        return None
    return resolver.resolve(cluster=cluster, database=database, table=relation)


def _route_function_name(
    route: ManagedTableRoute | None,
    relation: str,
    database: str,
) -> str:
    if route is not None and route.mode == "all_replicas":
        return "clusterAllReplicas"
    if route is None and (database.lower() == "system" or is_transfer_stage_identifier(relation)):
        return "clusterAllReplicas"
    return "cluster"


def _table_name_with_route(table: exp.Table, route: ManagedTableRoute) -> str:
    routed = table.copy()
    identifier = cast("exp.Identifier", routed.this)
    routed.set(
        "this",
        exp.to_identifier(
            route.table,
            quoted=bool(identifier.args.get("quoted")),
        ),
    )
    return routed.sql(dialect="clickhouse")


def _changes_table_metadata(statement: exp.Expression) -> bool:
    return isinstance(
        statement,
        (exp.Alter, exp.Create, exp.Detach, exp.Drop, exp.TruncateTable),
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
    function_name: str = "cluster",
) -> str:
    return _cluster_table(
        cluster,
        database,
        table,
        sharding_key=sharding_key,
        function_name=function_name,
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

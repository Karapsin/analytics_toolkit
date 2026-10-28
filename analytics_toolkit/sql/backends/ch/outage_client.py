"""Transport-independent enforcement of ClickHouse availability contracts."""

from __future__ import annotations

from typing import Any, cast

from sqlglot import exp, parse_one

from analytics_toolkit.sql.backends.utils import sql_string_literal
from analytics_toolkit.sql.connection.errors import (
    AmbiguousSqlReplaceError,
    ClickHouseClusterTopologyError,
    ClickHouseClusterUnavailableError,
)
from analytics_toolkit.sql.execution.backend_options import (
    backend_operation,
    current_backend_operation,
    current_backend_query_label,
)
from analytics_toolkit.sql.execution.labels import apply_query_label

from .outage_context import INSERT_KEY, INTERNAL_IO, WRITING, internal_io
from .outage_ddl import (
    _execute_local,
    copy_table_definition,
    create_on_healthy_hosts,
    create_target,
)
from .outage_ownership import assert_new_target, claim_private_create
from .outage_passthrough import modeled_statement
from .outage_session import OutageSession, configured_cluster, private_table
from .outage_topology import warn_degraded
from .routing import (
    _LOCAL_SQL_EXECUTION,
    ChClusterRouting,
    ClusterRoutingClient,
    _identifier_value,
    _RoutingClickHouse,
)

_CLUSTER_REQUIRED_ARGS = 3
_DESCRIBE_DEFAULT_FIELDS = 3

_DESTRUCTIVE = (exp.Alter, exp.Drop, exp.Detach, exp.Delete, exp.Update, exp.TruncateTable)
_READ_ONLY = (exp.Query, exp.Describe, exp.Show)


def _destructive(statement: exp.Expr) -> bool:
    return isinstance(statement, (*_DESTRUCTIVE, exp.Command)) or (
        isinstance(statement, exp.Create) and bool(statement.args.get("replace"))
    )


def _creates_data(statement: exp.Expr) -> bool:
    return (
        isinstance(statement, exp.Create)
        and isinstance(statement.args.get("expression"), exp.Query)
        and statement.find(exp.EmptyProperty) is None
    )


def _named_target(statement: exp.Expr, database: str | None) -> tuple[str, str] | None:
    target = statement.this
    tables = statement.args.get("tables") or statement.args.get("expressions")
    if target is None and isinstance(tables, list) and len(tables) == 1:
        target = tables[0]
    if isinstance(target, exp.Schema):
        target = target.this
    if isinstance(target, exp.Table) and isinstance(target.this, exp.Identifier):
        return target.db or database or "", target.name
    return None


def _cluster_names(statement: exp.Expr) -> list[str]:
    names = [str(node.this.this) for node in statement.find_all(exp.OnCluster)]
    for table in statement.find_all(exp.Table):
        function = table.this
        if (
            isinstance(function, exp.Anonymous)
            and function.name.lower()
            in {
                "cluster",
                "clusterallreplicas",
            }
            and function.expressions
        ):
            arguments = function.expressions
            values = [_identifier_value(value) for value in arguments[:_CLUSTER_REQUIRED_ARGS]]
            if len(values) != _CLUSTER_REQUIRED_ARGS or any(value is None for value in values):
                msg = "Outage policies require static cluster, database and table arguments."
                raise ClickHouseClusterTopologyError(msg)
            names.append(cast("str", values[0]))
    return list(dict.fromkeys(names))


class OutageClient:
    _ch_outage_client = True

    def __init__(self, client: Any, config: Any) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_session", None)
        state = current_backend_operation()
        object.__setattr__(
            self, "_connection_policy", state.ch_unavailable_shards if state else None
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._client, name, value)

    @property
    def cluster_routing(self) -> ChClusterRouting | None:
        return cast("ChClusterRouting | None", self._config.cluster_routing)

    def _get_session(self, state: Any) -> OutageSession:
        if self._session is None or self._session.state is not state:
            object.__setattr__(
                self,
                "_session",
                OutageSession(self._client, self._config, state, self._connection_policy),
            )
        return cast("OutageSession", self._session)

    def _delegate(self, session: OutageSession, routing: ChClusterRouting | None) -> Any:
        if routing is None or self._config.cluster_routing is None or _LOCAL_SQL_EXECUTION.get():
            return self._client
        routed = ClusterRoutingClient(self._client, routing=routing, database=self._config.database)
        object.__setattr__(routed, "_managed_pairs", session)
        return routed

    def outage_table_exists(self, table_name: str) -> bool:
        with backend_operation() as state:
            session = self._get_session(state)
            cluster = configured_cluster(self._config)
            table = parse_one(table_name, read=_RoutingClickHouse, into=exp.Table)
            target = (table.db or self._config.database, table.name)
            if state.resources.get(("ch_owned_local", self._config.connection_key, target)):
                cluster = None
            if cluster is None:
                result = self._client.query(f"EXISTS TABLE {table_name}")
                return bool(result.result_rows and result.result_rows[0][0])
            database = table.db or self._config.database
            if database is None:
                msg = "A database is required for cluster table inspection."
                raise ClickHouseClusterTopologyError(msg)
            scope = session.scope(cluster)
            objects = scope.objects(database, table.name) or session.local_facade(
                database, table.name
            )
            if objects:
                session.resolve(cluster=cluster, database=database, table=table.name)
            elif scope.objects(database, f"{table.name}_shard"):
                msg = (
                    f"Partial managed pair for {database}.{table.name}: the physical shard "
                    "table exists without its facade. Reconcile the pair before loading data."
                )
                raise ClickHouseClusterTopologyError(msg)
            elif session.policy == "stop" and not private_table(table.name):
                scope.require_all(scope.reachable, "Table absence preflight")
            return bool(objects)

    def outage_assert_new_target(
        self, table_name: str, *, policy: Any, cluster: str | None, only_shard: bool
    ) -> None:
        with backend_operation() as state:
            assert_new_target(
                self._get_session(state),
                table_name,
                policy=policy,
                cluster=cluster,
                only_shard=only_shard,
            )

    def sql_preflight_statements(self, sqls: Any) -> None:
        """Validate every required DDL host before a batch's local or remote mutation."""
        with backend_operation() as state:
            session = self._get_session(state)
            parsed = []
            clusters: list[str] = []
            for sql in sqls:
                modeled = modeled_statement(session, sql, parse_one)
                if modeled is None:
                    continue
                statement, special = modeled
                parsed.append(statement)
                clusters.extend(special[1] if special else _cluster_names(statement))
                engine = (
                    statement.find(exp.EngineProperty)
                    if isinstance(statement, exp.Create)
                    else None
                )
                function = engine.this if engine is not None else None
                if isinstance(function, exp.Anonymous) and function.name.lower() == "distributed":
                    cluster = _identifier_value(function.expressions[0])
                    if not cluster:
                        msg = "Cannot verify the Distributed routing cluster before creation."
                        raise ClickHouseClusterTopologyError(msg)
                    clusters.append(cluster)
            configured = self._config.cluster_routing
            if not clusters and configured is not None and not _LOCAL_SQL_EXECUTION.get():
                clusters.append(configured.cluster)
            clusters = list(dict.fromkeys(clusters))
            destructive = [item for item in parsed if _destructive(item)]
            for cluster in clusters:
                scope = session.scope(cluster)
                requires_full = any(
                    not (target is not None and self._owned_private(session, target))
                    for item in destructive
                    for target in [_named_target(item, self._config.database)]
                )
                if session.policy == "stop" or requires_full:
                    self._require_execution_hosts(session, scope, "Operation execution hosts")
                for statement in destructive:
                    self._preflight_destructive_tables(session, statement, cluster)
            for statement in destructive:
                self._check_destructive(session, _named_target(statement, self._config.database))

    def query(self, sql: str, **kwargs: Any) -> Any:
        return self._run("query", sql, kwargs)

    def command(self, sql: str, **kwargs: Any) -> Any:
        return self._run("command", sql, kwargs)

    def raw_query(self, sql: str, **kwargs: Any) -> Any:
        return self._run("raw_query", sql, kwargs)

    def query_df(self, sql: str, **kwargs: Any) -> Any:
        return self._run("query_df", sql, kwargs)

    def query_df_stream(self, sql: str, **kwargs: Any) -> Any:
        return self._run("query_df_stream", sql, kwargs)

    def insert(self, table: str, data: Any, column_names: Any, **kwargs: Any) -> Any:
        return self._binary("insert", table, data, column_names, kwargs)

    def insert_df(self, table: str, df: Any, column_names: Any = None, **kwargs: Any) -> Any:
        return self._binary("insert_df", table, df, column_names, kwargs)

    def _binary(
        self, method: str, table: str, data: Any, columns: Any, kwargs: dict[str, Any]
    ) -> Any:
        statement = parse_one(f"INSERT INTO {table} VALUES (0)", read=_RoutingClickHouse)  # noqa: S608 - escaped literals or parsed SQL identifiers
        if INTERNAL_IO.get():
            return getattr(self._client, method)(table, data, column_names=columns, **kwargs)
        with backend_operation() as state:
            session = self._get_session(state)
            token = WRITING.set(True)
            target = _named_target(statement, self._config.database)
            try:
                session.insert_scope = None
                routing = session.infer_routing(statement)
                delegate = self._delegate(session, routing)
                if routing is not None and target is not None:
                    session.resolve(cluster=routing.cluster, database=target[0], table=target[1])
                settings = session.insertion_settings()
                self._settings(session, kwargs, settings)
                state.require_source_snapshot(self._config.connection_key)
                is_private = target is not None and self._owned_private(session, target)
                if not is_private:
                    state.live_submitted = True
                return getattr(delegate, method)(table, data, column_names=columns, **kwargs)
            except BaseException as exc:
                if state.live_submitted:
                    state.preserve_recovery = True
                    exc.__dict__["analytics_toolkit_sql_retry_safe"] = False
                raise
            finally:
                WRITING.reset(token)

    def _run(self, method: str, sql: str, kwargs: dict[str, Any]) -> Any:
        if INTERNAL_IO.get():
            return getattr(self._client, method)(sql, **kwargs)
        with backend_operation() as state:
            session = self._get_session(state)
            sql = apply_query_label(sql, current_backend_query_label())
            modeled = modeled_statement(session, sql, parse_one)
            if modeled is None:
                return getattr(self._client, method)(sql, **kwargs)
            statement, special = modeled
            return self._execute_statement(
                method,
                sql,
                kwargs,
                state=state,
                session=session,
                statement=statement,
                special=special,
            )

    def _execute_statement(  # noqa: C901, PLR0912, PLR0913, PLR0915 - one guarded submission boundary
        self,
        method: str,
        sql: str,
        kwargs: dict[str, Any],
        *,
        state: Any,
        session: OutageSession,
        statement: exp.Expr,
        special: Any,
    ) -> Any:
        writing = isinstance(statement, exp.Insert)
        destructive = _destructive(statement) or special is not None
        token = WRITING.set(writing)
        key_token = INSERT_KEY.set(None)
        target = _named_target(statement, self._config.database)
        submitted = False
        try:
            routing: ChClusterRouting | None
            if writing:
                session.insert_scope = None
            source = statement.args.get("expression")
            if isinstance(source, exp.Query) and (writing or isinstance(statement, exp.Create)):
                source_token = WRITING.set(False)
                source_local_token = _LOCAL_SQL_EXECUTION.set(False)
                try:
                    source_routing = session.infer_routing(source)
                    explicit_sources = _cluster_names(source)
                    if source_routing is None and explicit_sources:
                        source_routing = ChClusterRouting(explicit_sources[0])
                    source_delegate = self._delegate(session, source_routing)
                    if isinstance(source_delegate, ClusterRoutingClient):
                        source = parse_one(
                            source_delegate.route(source.sql(dialect="clickhouse")),
                            read=_RoutingClickHouse,
                        )
                        statement.set("expression", source)
                        sql = statement.sql(dialect="clickhouse")
                finally:
                    _LOCAL_SQL_EXECUTION.reset(source_local_token)
                    WRITING.reset(source_token)
            function_target = self._function_target(statement) if writing else None
            if function_target is not None:
                cluster, db, name, function_key = function_target
                INSERT_KEY.set(function_key)
                target = (db, name)
                if self._config.cluster_routing is not None:
                    replacement = exp.Table(this=exp.to_identifier(name), db=exp.to_identifier(db))
                    if isinstance(statement.this, exp.Schema):
                        statement.this.set("this", replacement)
                    else:
                        statement.set("this", replacement)
                    statement.set("is_function", value=False)
                    sql = statement.sql(dialect="clickhouse")
                routing = ChClusterRouting(cluster, sharding_key=function_key)
            else:
                routing = None if _LOCAL_SQL_EXECUTION.get() else session.infer_routing(statement)
            mutation = writing or destructive or isinstance(statement, exp.Create)
            clusters = (
                special[1]
                if special is not None
                else (
                    [str(node.this.this) for node in statement.find_all(exp.OnCluster)]
                    if mutation
                    else _cluster_names(statement)
                )
            )
            if not clusters and routing is not None:
                clusters = [routing.cluster]
            for cluster in clusters:
                scope = session.scope(cluster)
                if (isinstance(statement, exp.Create) or destructive) and session.policy == "stop":
                    scope.require_all(scope.reachable, "DDL preflight")
            if (
                isinstance(statement, exp.Create)
                and clusters
                and session.policy == "healthy_shards"
            ):
                statement, populate = self._expand_create(statement)
                created = create_on_healthy_hosts(session.scope(clusters[0]), statement)
                self._register_owned(session, statement, new=created)
                if populate is not None and created:
                    db, name = create_target(statement, self._config.database)
                    table_sql = exp.Table(
                        this=exp.to_identifier(name, quoted=True),
                        db=exp.to_identifier(db, quoted=True),
                    ).sql(dialect="clickhouse")
                    self.command(f"INSERT INTO {table_sql} {populate}")
                return None
            if isinstance(statement, exp.Create) and target is not None:
                # Suppress the toolkit's local fallback after a healthy-only
                # cluster create; it must not expand the selected DDL scope.
                key = ("ch_create", self._config.connection_key)
                if session.policy == "healthy_shards" and any(
                    resource[:2] == key and resource[-1] == f"{target[0]}.{target[1]}"
                    for resource in state.resources
                    if isinstance(resource, tuple)
                ):
                    return None
            if destructive:
                configured = self._config.cluster_routing
                preflight_clusters = clusters or (
                    [configured.cluster] if configured is not None else []
                )
                for cluster in preflight_clusters:
                    scope = session.scope(cluster)
                    owned = target is not None and self._owned_private(session, target)
                    if not owned:
                        self._require_execution_hosts(session, scope, "Destructive execution hosts")
                for cluster in preflight_clusters:
                    self._preflight_destructive_tables(session, statement, cluster)
                self._check_destructive(session, target)
                if (
                    session.policy == "healthy_shards"
                    and clusters
                    and target is not None
                    and self._owned_private(session, target)
                ):
                    local_statement = statement.copy()
                    local_statement.set("cluster", None)
                    for cluster in clusters:
                        scope = session.scope(cluster)
                        for host in scope.reachable:
                            _execute_local(scope, host, local_statement.sql(dialect="clickhouse"))
                        if isinstance(statement, exp.Drop):
                            state.resources.pop(
                                (
                                    "ch_create",
                                    self._config.connection_key,
                                    scope.cluster,
                                    f"{target[0]}.{target[1]}",
                                ),
                                None,
                            )
                    return None
            if clusters and routing is None and not mutation and not _LOCAL_SQL_EXECUTION.get():
                routing = ChClusterRouting(clusters[0])
            if (
                _creates_data(statement)
                and not clusters
                and session.policy == "healthy_shards"
                and state.partial
            ):
                msg = (
                    "Partial-source local CTAS requires separate CREATE and INSERT SELECT "
                    "statements, or an explicit target ON CLUSTER."
                )
                raise ClickHouseClusterTopologyError(msg)  # noqa: TRY301 - preserve relay artifacts
            delegate = self._delegate(session, routing)
            execution_sql = (
                delegate.route(sql)
                if isinstance(delegate, ClusterRoutingClient) and special is None
                else sql
            )
            execution_sql = self._metadata_scope_sql(session, statement, execution_sql)
            if writing:
                if routing is not None and target is not None:
                    session.resolve(cluster=routing.cluster, database=target[0], table=target[1])
                if function_target is not None:
                    session.insert_key = function_target[3]
                settings = session.insertion_settings()
                self._settings(session, kwargs, settings)
                self._check_inline_settings(statement, settings)
                if (
                    isinstance(statement.expression, exp.Query)
                    and session.policy == "healthy_shards"
                    and state.partial
                ):
                    return self._relay_insert(cast("exp.Insert", statement), state)
            else:
                partial = any(
                    not item["complete"]
                    for item in state.statuses.values()
                    if item["alias"] == self._config.connection_key
                )
                settings = {
                    "skip_unavailable_shards": int(session.policy == "healthy_shards" and partial)
                }
            if (destructive or isinstance(statement, exp.Create)) and clusters:
                settings.update(
                    {
                        "distributed_ddl_task_timeout": 300,
                        "distributed_ddl_output_mode": "throw",
                    }
                )
            self._settings(session, kwargs, settings)
            self._check_inline_settings(statement, settings)
            if not writing and not destructive and clusters and routing is None:
                # Readiness probes deliberately bypass SQL rewriting, but
                # still use policy-controlled availability settings.
                for cluster in clusters:
                    scope = session.scope(cluster)
                    if session.policy == "stop":
                        scope.require_all(scope.reachable, "Cluster metadata")
            claim_private_create(session, statement, target, clusters)
            private = target is not None and self._owned_private(session, target)
            state.require_source_snapshot(self._config.connection_key)
            if (writing or destructive or _creates_data(statement)) and not private:
                state.live_submitted = True
            submitted = True
            return getattr(self._client, method)(execution_sql, **kwargs)
        except BaseException as exc:
            if state.live_submitted:
                state.preserve_recovery = True
                exc.__dict__["analytics_toolkit_sql_retry_safe"] = False
                if destructive and submitted and isinstance(exc, Exception):
                    msg = (
                        "ClickHouse destructive DDL may be partially applied; "
                        "no replay or blind rollback "
                        "is safe. Recovery tables were preserved."
                    )
                    raise AmbiguousSqlReplaceError(msg) from exc
            raise
        finally:
            WRITING.reset(token)
            INSERT_KEY.reset(key_token)

    @staticmethod
    def _function_target(statement: exp.Expr) -> tuple[str, str, str, str] | None:
        target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
        function = target.this if isinstance(target, exp.Table) else target
        if not isinstance(function, exp.Anonymous):
            return None
        arguments = function.expressions
        if (
            function.name.lower() not in {"cluster", "clusterallreplicas"}
            or len(arguments) < _CLUSTER_REQUIRED_ARGS
        ):
            msg = "Outage policies require a named or cluster-function insertion target."
            raise ClickHouseClusterTopologyError(msg)
        values = [_identifier_value(item) for item in arguments[:_CLUSTER_REQUIRED_ARGS]]
        if any(value is None for value in values):
            msg = "Cluster insertion arguments must be static identifiers."
            raise ClickHouseClusterTopologyError(msg)
        key = (
            arguments[3].sql(dialect="clickhouse")
            if len(arguments) > _CLUSTER_REQUIRED_ARGS
            else "rand()"
        )
        return cast("str", values[0]), cast("str", values[1]), cast("str", values[2]), key

    def _expand_create(self, statement: exp.Create) -> tuple[exp.Create, str | None]:
        query = statement.args.get("expression")
        if query is None:
            return statement, None
        if isinstance(statement.this, exp.Schema):
            msg = (
                "Healthy-only CREATE AS with an explicit schema is unsupported. "
                "Use separate CREATE and population steps to preserve explicit types, "
                "names, defaults, constraints, and indexes."
            )
            raise ClickHouseClusterTopologyError(msg)
        if isinstance(query, exp.Table):
            with backend_operation() as state:
                session = self._get_session(state)
                routing = session.infer_routing(exp.select("*").from_(query.copy()))
                cluster = (
                    routing.cluster if routing is not None else configured_cluster(self._config)
                )
                database = query.db or self._config.database or ""
                objects = (
                    session.scope(cluster).objects(database, query.name)
                    if cluster is not None
                    else session.local_objects(database, query.name)
                )
                if not objects:
                    msg = "Cannot verify the source definition for schema-only table creation."
                    raise ClickHouseClusterTopologyError(msg)
                return copy_table_definition(statement, objects[0]), None
        if not isinstance(query, exp.Query):
            msg = "Cannot safely infer CREATE AS schema."
            raise ClickHouseClusterTopologyError(msg)
        result = self.query(
            f"SELECT * FROM ({query.sql(dialect='clickhouse')}) AS _atk_schema LIMIT 0"  # noqa: S608 - escaped literals or parsed SQL identifiers
        )
        columns = []
        for name, kind in zip(result.column_names, result.column_types):
            type_name = getattr(kind, "name", str(kind))
            columns.append(
                exp.ColumnDef(
                    this=exp.to_identifier(name, quoted=True),
                    kind=exp.DataType.build(type_name, dialect="clickhouse"),
                )
            )
        converted = statement.copy()
        target = converted.this.this if isinstance(converted.this, exp.Schema) else converted.this
        converted.set("this", exp.Schema(this=target.copy(), expressions=columns))
        converted.set("expression", None)
        empty = converted.find(exp.EmptyProperty) is not None
        properties = converted.args.get("properties")
        if properties is not None:
            properties.set(
                "expressions",
                [
                    item
                    for item in properties.expressions
                    if not isinstance(item, exp.EmptyProperty)
                ],
            )
        return converted, None if empty else query.sql(dialect="clickhouse")

    def _relay_insert(self, statement: exp.Insert, state: Any) -> None:
        """Keep partial source reads separate from loss-intolerant destination delivery."""
        target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
        if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
            msg = "Ad hoc INSERT SELECT requires a named destination."
            raise ClickHouseClusterTopologyError(msg)
        table_sql = target.sql(dialect="clickhouse")
        if isinstance(statement.this, exp.Schema):
            columns = [item.name for item in statement.this.expressions]
        else:
            description = self.query(f"DESCRIBE TABLE {table_sql}")
            columns = [
                row[0]
                for row in description.result_rows
                if len(row) < _DESCRIBE_DEFAULT_FIELDS or row[2] not in {"MATERIALIZED", "ALIAS"}
            ]
        from analytics_toolkit.sql.connection.get_sql_connection import (  # noqa: PLC0415 - factory imports backend
            get_sql_connection,
        )

        with internal_io():
            delivery_connection = get_sql_connection(self._config.connection_key)
        writer = OutageClient(delivery_connection, self._config)
        writer._connection_policy = self._get_session(state).policy
        try:
            with self.query_df_stream(statement.expression.sql(dialect="clickhouse")) as frames:
                for frame in frames:
                    if len(frame.columns) != len(columns):
                        msg = "INSERT SELECT column counts do not match."
                        raise ClickHouseClusterTopologyError(msg)  # noqa: TRY301 - preserve relay artifacts
                    insert_frame = frame.copy()
                    insert_frame.columns = columns
                    writer.insert_df(table_sql, insert_frame, column_names=columns)
        except BaseException as exc:
            if state.live_submitted:
                state.preserve_recovery = True
                exc.__dict__["analytics_toolkit_sql_retry_safe"] = False
            raise
        finally:
            delivery_connection.close()

    def outage_after_create(
        self, table_name: str, *, policy: Any, pair: bool, expected_columns: Any = None
    ) -> bool:
        with backend_operation() as state:
            session = self._get_session(state)
            if session.policy != "healthy_shards" or policy is None:
                return False
            from .ddl import build_ch_shard_table_name  # noqa: PLC0415 - DDL imports adapter
            from .wait import (  # noqa: PLC0415 - wait imports client
                _normalize_ch_type_for_comparison,
            )

            roles = [
                (
                    build_ch_shard_table_name(table_name) if pair else table_name,
                    policy.shard_on_cluster,
                )
            ]
            if pair:
                roles.append((table_name, policy.distributed_on_cluster))
            for name, cluster in roles:
                if cluster is None:
                    result = self._client.query(f"EXISTS TABLE {name}")
                    if not result.result_rows or not result.result_rows[0][0]:
                        msg = f"Local table {name} is not ready."
                        raise ClickHouseClusterUnavailableError(msg)
                    continue
                scope = session.scope(cluster)
                table = parse_one(name, read=_RoutingClickHouse, into=exp.Table)
                objects = scope.objects(table.db or self._config.database, table.name)
                if {item.host.position for item in objects} != {
                    host.position for host in scope.reachable
                }:
                    msg = f"Table {name} is not ready on selected hosts."
                    raise ClickHouseClusterUnavailableError(msg)
                if expected_columns:
                    parsed = parse_one(objects[0].create_sql, read=_RoutingClickHouse)
                    actual = {
                        column.name: _normalize_ch_type_for_comparison(
                            column.args["kind"].sql(dialect="clickhouse")
                        )
                        for column in parsed.this.expressions
                        if isinstance(column, exp.ColumnDef)
                    }
                    if any(
                        actual.get(column) != _normalize_ch_type_for_comparison(kind)
                        for column, kind in expected_columns.items()
                    ):
                        msg = f"Unexpected schema for {name}."
                        raise ClickHouseClusterTopologyError(msg)
            return True

    def _register_owned(self, session: OutageSession, statement: exp.Create, *, new: bool) -> None:
        # IF NOT EXISTS on a pre-existing table never establishes cleanup ownership.
        target = _named_target(statement, self._config.database)
        if new and target is not None and private_table(target[1]):
            session.state.resources[("ch_owned", self._config.connection_key, target)] = True

    def _owned_private(self, session: OutageSession, target: tuple[str, str]) -> bool:
        return private_table(target[1]) and bool(
            session.state.resources.get(("ch_owned", self._config.connection_key, target))
        )

    def _require_execution_hosts(self, session: OutageSession, scope: Any, reason: str) -> None:
        if session.policy == "healthy_shards" and len(scope.reachable) != len(scope.hosts):
            warn_degraded(
                session.state,
                self._config,
                scope.record(scope.reachable),
                "blocked destructive operation",
            )
        scope.require_all(scope.reachable, reason)

    def _preflight_destructive_tables(
        self, session: OutageSession, statement: exp.Expr, cluster: str
    ) -> None:
        scope = session.scope(cluster)
        for table in statement.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                msg = "Destructive table operations require static table names."
                raise ClickHouseClusterTopologyError(msg)
            database = table.db or self._config.database
            objects = scope.objects(database, table.name) if database else []
            if (
                isinstance(statement, exp.Drop)
                and statement.args.get("kind") == "TABLE"
                and statement.args.get("exists")
            ):
                # All execution hosts were checked before this table inspection.
                # Missing schema is the explicit intent of IF EXISTS, not a missing host.
                continue
            if objects and database:
                if objects[0].engine.lower().startswith(
                    ("replicated", "shared")
                ) and not self._owned_private(session, (database, table.name)):
                    scope.require_all(
                        [item.host for item in objects], "Destructive table readiness"
                    )
                token = WRITING.set(False)
                try:
                    session.resolve(cluster=cluster, database=database, table=table.name)
                finally:
                    WRITING.reset(token)

    def _check_destructive(self, session: OutageSession, target: tuple[str, str] | None) -> None:
        if session.state.preserve_recovery:
            msg = "Recovery artifacts must be retained after an uncertain mutation."
            raise ClickHouseClusterUnavailableError(msg)
        owned = target is not None and self._owned_private(session, target)
        if session.state.partial and not owned:
            msg = "Destructive operations require complete ClickHouse coverage."
            raise ClickHouseClusterUnavailableError(msg)

    def _settings(
        self, session: OutageSession, kwargs: dict[str, Any], required: dict[str, Any]
    ) -> None:
        if any(
            "skip_unavailable_shards_mode" in scope.probe_settings
            for scope in session.scopes.values()
        ):
            required["skip_unavailable_shards_mode"] = "unavailable_or_table_missing"
        supplied = dict(kwargs.get("settings") or {})
        configured = dict(self._config.settings or {})
        for name, value in required.items():
            if name in {
                "skip_unavailable_shards",
                "distributed_foreground_insert",
                "async_insert",
                "insert_shard_id",
            }:
                if name in configured and str(configured[name]).lower() not in {
                    str(value).lower(),
                    str(bool(value)).lower(),
                }:
                    msg = f"Connection setting {name!r} contradicts the outage policy."
                    raise ClickHouseClusterTopologyError(msg)
                if name in supplied and supplied[name] != value:
                    msg = f"Query setting {name!r} contradicts the outage policy."
                    raise ClickHouseClusterTopologyError(msg)
            supplied[name] = value
        if configured.get("skip_unavailable_shards_mode") not in {
            None,
            "unavailable",
            "unavailable_or_table_missing",
        }:
            msg = "A permissive shard-exception mode would hide SQL failures."
            raise ClickHouseClusterTopologyError(msg)
        kwargs["settings"] = supplied

    @staticmethod
    def _check_inline_settings(statement: exp.Expr, required: dict[str, Any]) -> None:
        settings = []
        for node in statement.walk():
            values = node.args.get("settings")
            if isinstance(values, list):
                settings.extend(values)
            if isinstance(node, exp.SettingsProperty):
                settings.extend(node.find_all(exp.EQ))
        for equality in settings:
            if not isinstance(equality, exp.EQ):
                continue
            name = equality.left.name
            actual = equality.right.this
            if isinstance(equality.right, exp.Boolean):
                actual = int(actual)
            if name in required and str(actual) != str(required[name]):
                msg = f"SQL setting {name!r} contradicts the outage policy."
                raise ClickHouseClusterTopologyError(msg)

    def _metadata_scope_sql(self, session: OutageSession, statement: exp.Expr, sql: str) -> str:  # noqa: C901, PLR0912 - metadata statements share one selected scope
        if not _LOCAL_SQL_EXECUTION.get():
            return sql
        default = configured_cluster(self._config)
        target = _named_target(statement, self._config.database)
        if (
            default
            and isinstance(statement, exp.Describe)
            and target
            and not session.state.resources.get(
                ("ch_owned_local", self._config.connection_key, target)
            )
        ):
            route = session.resolve(cluster=default, database=target[0], table=target[1])
            return (
                f"DESCRIBE TABLE cluster({sql_string_literal(session.scope(default).cluster)}, "
                f"{sql_string_literal(route.database)}, {sql_string_literal(route.table)})"
            )
        if not isinstance(statement, exp.Select):
            return sql
        for table in statement.find_all(exp.Table):
            if (
                table.db == "system"
                and table.name == "clusters"
                and session.policy == "healthy_shards"
            ):
                where = statement.args.get("where")
                if where is None:
                    continue
                for equality in where.find_all(exp.EQ):
                    if equality.left.name == "cluster" and isinstance(equality.right, exp.Literal):
                        scope = session.scope(str(equality.right.this))
                        positions = ", ".join(
                            f"({host.shard}, {host.replica})" for host in scope.reachable
                        )
                        statement.where(
                            f"(shard_num, replica_num) IN ({positions})", append=True, copy=False
                        )
                        return statement.sql(dialect="clickhouse")
            if table.db == "system" and table.name in {"tables", "columns"} and default:
                where = statement.args.get("where")
                if where is None:
                    continue
                values = {
                    equality.left.name: str(equality.right.this)
                    for equality in where.find_all(exp.EQ)
                    if isinstance(equality.right, exp.Literal)
                }
                name = values.get("table") or values.get("name")
                database = values.get("database") or self._config.database
                if not name or not database:
                    continue
                if session.state.resources.get(
                    ("ch_owned_local", self._config.connection_key, (database, name))
                ):
                    continue
                scope = session.scope(default)
                objects = scope.objects(database, name)
                if not objects:
                    continue
                session.resolve(cluster=default, database=database, table=name)
                replacement = parse_one(
                    f"SELECT * FROM cluster({sql_string_literal(scope.cluster)}, "  # noqa: S608 - escaped literals or parsed SQL identifiers
                    f"system, {table.name})",
                    read=_RoutingClickHouse,
                ).find(exp.Table)
                if replacement is None:
                    msg = "Cannot resolve metadata table function."
                    raise ClickHouseClusterTopologyError(msg)
                table.replace(replacement.copy())
                statement.where(f"_shard_num = {objects[0].host.shard}", append=True, copy=False)
                return statement.sql(dialect="clickhouse")
        return sql

    def close(self) -> None:
        self._client.close()

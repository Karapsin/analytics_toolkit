"""Transport-independent enforcement of ClickHouse availability contracts."""

from __future__ import annotations

from typing import Any, cast

from sqlglot import exp, parse_one

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

from .cluster_functions import (
    canonicalize_cluster_functions,
    function_insert_target,
    insert_coverage_intent,
)
from .cluster_functions import cluster_names as _cluster_names
from .dialect import sql_string_literal  # noqa: F401 - preserve the historical helper import.
from .outage_context import (
    INSERT_KEY,
    INTERNAL_IO,
    LOGICAL_STATISTICS,
    PRESERVE_SOURCE_SCOPE,
    WRITING,
    internal_io,
)
from .outage_ddl import (
    _execute_local,
    copy_table_definition,
    create_on_healthy_hosts,
    create_target,
)
from .outage_layout import inspect_outage_layout
from .outage_ownership import (
    assert_new_target,
    claim_private_create,
    owned_local,
    owned_private,
    retire_host_drop,
)
from .outage_passthrough import modeled_statement
from .outage_preflight import is_facade_create
from .outage_session import OutageSession, configured_cluster, private_table
from .outage_topology import execution_settings, warn_degraded
from .replacement_cutover import mark_cutover_submitted, preflight_cutover
from .routing import (
    _LOCAL_SQL_EXECUTION,
    ChClusterRouting,
    ClusterRoutingClient,
    _RoutingClickHouse,
)
from .upsert_context import certified_partition_rebuild_alter, verified_local_partition_alter
from .upsert_storage import apply_upsert_write_key, upsert_write_scope

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
            if self._config.cluster_routing is None:
                return session.native_table_exists(table_name, table)
            target = (table.db or self._config.database, table.name)
            if owned_local(session, target):
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

    def outage_inspect_existing_layout(self, table_name: str) -> Any:
        with backend_operation() as state:
            return inspect_outage_layout(self._get_session(state), table_name)

    def outage_certify_logical_storage(self, cluster: str, database: str, table: str) -> None:
        """Certify complete physical coverage and shared replica identity before preservation."""
        with backend_operation() as state:
            scope = self._get_session(state).scope(cluster)
            objects = scope.objects(database, table)
            if not objects or not objects[0].engine.casefold().startswith(("replicated", "shared")):
                message = "Logical storage certification requires verified replicated storage."
                raise ClickHouseClusterTopologyError(message)
            selected = scope.usable(
                objects,
                f"{database}.{table}",
                identity=(database, table),
                coverage_intent="logical_shards",
            )
            scope.require_all(selected, "Replicated upsert preservation")

    def sql_preflight_statements(self, sqls: Any) -> None:
        """Validate every required DDL host before a batch's local or remote mutation."""
        from .outage_preflight import preflight_statements  # noqa: PLC0415 -- client cycle.

        preflight_statements(self, sqls)

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
            except BaseException:
                if state.live_submitted:
                    state.preserve_recovery = True
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
                from .coordinator_database import (  # noqa: PLC0415
                    changes_current_database,
                    invalidate_current_database,
                )

                namespace_change = changes_current_database(sql)
                result = getattr(self._client, method)(sql, **kwargs)
                if namespace_change:
                    invalidate_current_database(self._client)
                return result
            statement, special = modeled
            from .coordinator_database import resolve_current_database  # noqa: PLC0415

            if canonicalize_cluster_functions(
                statement,
                self._config.database,
                database_resolver=lambda: resolve_current_database(self._client),
            ):
                sql = statement.sql(dialect=_RoutingClickHouse)
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
        certified_partition = certified_partition_rebuild_alter(self, statement)
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
                source_local_token = _LOCAL_SQL_EXECUTION.set(PRESERVE_SOURCE_SCOPE.get())
                try:
                    source_routing = session.infer_routing(source)
                    explicit_sources = _cluster_names(source)
                    if source_routing is None and explicit_sources:
                        source_routing = ChClusterRouting(explicit_sources[0])
                    source_delegate = self._delegate(session, source_routing)
                    if isinstance(source_delegate, ClusterRoutingClient):
                        source = parse_one(
                            source_delegate.route(
                                source.sql(dialect=_RoutingClickHouse), upsert_connection=self
                            ),
                            read=_RoutingClickHouse,
                        )
                        statement.set("expression", source)
                        sql = statement.sql(dialect=_RoutingClickHouse)
                finally:
                    _LOCAL_SQL_EXECUTION.reset(source_local_token)
                    WRITING.reset(source_token)
            function_target = self._function_target(statement) if writing else None
            function_intent = (
                insert_coverage_intent(
                    cast("exp.Expression", statement),
                    automatic_routing=self._config.cluster_routing is not None,
                )
                if writing
                else None
            )
            if function_target is not None:
                cluster, db, name, function_key = function_target
                INSERT_KEY.set(function_key)
                target = (db, name)
                if self._config.cluster_routing is not None and not _LOCAL_SQL_EXECUTION.get():
                    replacement = exp.Table(this=exp.to_identifier(name), db=exp.to_identifier(db))
                    if isinstance(statement.this, exp.Schema):
                        statement.this.set("this", replacement)
                    else:
                        statement.set("this", replacement)
                    statement.set("is_function", value=False)
                    sql = statement.sql(dialect=_RoutingClickHouse)
                routing = ChClusterRouting(cluster, sharding_key=function_key)
            else:
                routing = None if _LOCAL_SQL_EXECUTION.get() else session.infer_routing(statement)
            routing, target = upsert_write_scope(self, statement, routing, target)
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
                if isinstance(statement, exp.Create) and session.policy == "stop":
                    if is_facade_create(statement):
                        scope.require_available(scope.reachable, "DDL preflight")
                    else:
                        scope.require_all(scope.reachable, "Physical DDL preflight")
                elif destructive and session.policy == "stop":
                    scope.require_all(scope.reachable, "DDL preflight")
            if (
                isinstance(statement, exp.Create)
                and clusters
                and (
                    session.policy == "healthy_shards"
                    or (
                        is_facade_create(statement)
                        and session.scope(clusters[0]).availability_optional
                        and len(session.scope(clusters[0]).reachable)
                        != len(session.scope(clusters[0]).hosts)
                    )
                )
            ):
                statement, populate = self._expand_create(statement)
                created = create_on_healthy_hosts(session.scope(clusters[0]), statement)
                self._register_owned(session, statement, new=created)
                if populate is not None and created:
                    db, name = create_target(statement, self._config.database)
                    table_sql = exp.Table(
                        this=exp.to_identifier(name, quoted=True),
                        db=exp.to_identifier(db, quoted=True),
                    ).sql(dialect=_RoutingClickHouse)
                    self.command(f"INSERT INTO {table_sql} {populate}")
                return None
            if isinstance(statement, exp.Create) and target is not None:
                key = ("ch_create", self._config.connection_key)
                if session.policy == "healthy_shards" and any(
                    resource[:2] == key and resource[-1] == f"{target[0]}.{target[1]}"
                    for resource in state.resources
                    if isinstance(resource, tuple)
                ):
                    return None
            if destructive:
                controlled_cutover = preflight_cutover(self, statement, clusters)
                configured = self._config.cluster_routing
                preflight_clusters = clusters or (
                    [configured.cluster]
                    if configured is not None
                    and not controlled_cutover
                    and not (
                        _LOCAL_SQL_EXECUTION.get()
                        and verified_local_partition_alter(self, statement)
                    )
                    and not (target is not None and owned_local(session, target))
                    else []
                )
                for cluster in preflight_clusters:
                    scope = session.scope(cluster)
                    owned = target is not None and self._owned_private(session, target)
                    if not owned:
                        self._require_execution_hosts(session, scope, "Destructive execution hosts")
                for cluster in preflight_clusters:
                    self._preflight_destructive_tables(session, statement, cluster)
                self._check_destructive(session, target, certified_partition=certified_partition)
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
                            _execute_local(
                                scope, host, local_statement.sql(dialect=_RoutingClickHouse)
                            )
                            if isinstance(statement, exp.Drop):
                                retire_host_drop(
                                    session, target, scope.observed_instances[host.position]
                                )
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
                delegate.route(sql, upsert_connection=self)
                if isinstance(delegate, ClusterRoutingClient) and special is None
                else sql
            )
            execution_sql = self._metadata_scope_sql(session, statement, execution_sql)
            if writing:
                if routing is not None and target is not None:
                    session.resolve(
                        cluster=routing.cluster,
                        database=target[0],
                        table=target[1],
                        coverage_intent=function_intent,
                    )
                if function_target is not None:
                    session.insert_key = function_target[3]
                apply_upsert_write_key(self, session, target)
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
                    "skip_unavailable_shards": int(
                        LOGICAL_STATISTICS.get() or (session.policy == "healthy_shards" and partial)
                    )
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
                for cluster in clusters:
                    scope = session.scope(cluster)
                    if session.policy == "stop":
                        scope.require_all(scope.reachable, "Cluster metadata")
            claim_private_create(session, statement, target, clusters)
            private = target is not None and self._owned_private(session, target)
            state.require_source_snapshot(self._config.connection_key)
            if (writing or destructive or _creates_data(statement)) and not private:
                state.live_submitted = True
            drop_identity = None
            if isinstance(statement, exp.Drop) and private and not clusters:
                from .drop_layout import coordinator_identity  # noqa: PLC0415 - native ACK cycle.

                drop_identity = coordinator_identity(self._client)
            submitted = True
            mark_cutover_submitted(self, statement, clusters)
            result = getattr(self._client, method)(execution_sql, **kwargs)
        except BaseException as exc:
            if state.live_submitted:
                state.preserve_recovery = True
                structural = (
                    destructive
                    and not isinstance(statement, (exp.Delete, exp.Update))
                    and not certified_partition
                )
                if structural and submitted:
                    state.structural_replay_blocked = True
                    exc.__dict__["analytics_toolkit_sql_retry_safe"] = False
                if structural and submitted and isinstance(exc, Exception):
                    msg = (
                        "ClickHouse destructive DDL may be partially applied; "
                        "no replay or blind rollback "
                        "is safe. Recovery tables were preserved."
                    )
                    raise AmbiguousSqlReplaceError(msg) from exc
            raise
        else:
            if drop_identity is not None and target is not None:
                retire_host_drop(session, target, drop_identity)
            return result
        finally:
            WRITING.reset(token)
            INSERT_KEY.reset(key_token)

    @staticmethod
    def _function_target(statement: exp.Expr) -> tuple[str, str, str, str] | None:
        return function_insert_target(statement)

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
                    kind=exp.DataType.build(type_name, dialect=_RoutingClickHouse),
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
        return converted, None if empty else query.sql(dialect=_RoutingClickHouse)

    def _relay_insert(self, statement: exp.Insert, state: Any) -> None:
        """Keep partial source reads separate from loss-intolerant destination delivery."""
        target = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
        if not isinstance(target, exp.Table) or not isinstance(target.this, exp.Identifier):
            msg = "Ad hoc INSERT SELECT requires a named destination."
            raise ClickHouseClusterTopologyError(msg)
        table_sql = target.sql(dialect=_RoutingClickHouse)
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
            with self.query_df_stream(
                statement.expression.sql(dialect=_RoutingClickHouse)
            ) as frames:
                for frame in frames:
                    if len(frame.columns) != len(columns):
                        msg = "INSERT SELECT column counts do not match."
                        raise ClickHouseClusterTopologyError(msg)  # noqa: TRY301 - preserve relay artifacts
                    insert_frame = frame.copy()
                    insert_frame.columns = columns
                    writer.insert_df(table_sql, insert_frame, column_names=columns)
        except BaseException:
            if state.live_submitted:
                state.preserve_recovery = True
            raise
        finally:
            from analytics_toolkit.sql.dml.transfer.runtime.retry import (  # noqa: PLC0415 - factory imports this client
                close_connection_ref,
            )

            close_connection_ref(
                {"connection": delivery_connection}, self._config.connection_key, "relay"
            )

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
                            column.args["kind"].sql(dialect=_RoutingClickHouse)
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
        # Actual host ACK capture publishes generation-bound receipts separately.
        del session, statement, new

    def _owned_private(self, session: OutageSession, target: tuple[str, str]) -> bool:
        return owned_private(session, target)

    def _require_execution_hosts(self, session: OutageSession, scope: Any, reason: str) -> None:
        status = scope.record(scope.reachable, coverage_intent="execution_hosts")
        if session.policy == "healthy_shards" and len(scope.reachable) != len(scope.hosts):
            warn_degraded(
                session.state,
                self._config,
                status,
                "blocked destructive operation",
            )
        scope.require_all(scope.reachable, reason)

    def _preflight_destructive_tables(
        self, session: OutageSession, statement: exp.Expr, cluster: str
    ) -> None:
        if preflight_cutover(self, statement, [cluster]):
            return
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

    def _check_destructive(
        self,
        session: OutageSession,
        target: tuple[str, str] | None,
        *,
        certified_partition: bool = False,
    ) -> None:
        del certified_partition
        if session.state.structural_replay_blocked:
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
        kwargs["settings"] = execution_settings(session.scopes.values(), supplied, required)

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

    def _metadata_scope_sql(self, session: OutageSession, statement: exp.Expr, sql: str) -> str:
        from .outage_metadata import metadata_scope_sql  # noqa: PLC0415

        return metadata_scope_sql(
            self._config, session, statement, sql, _named_target(statement, self._config.database)
        )

    def close(self) -> None:
        self._client.close()

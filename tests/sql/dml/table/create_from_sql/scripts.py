from __future__ import annotations

import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, route_sql
from analytics_toolkit.sql.backends.models import SourceColumn
from analytics_toolkit.sql.dml.table import source_script

from tests.sql._support.create_from_sql import (
    SOURCE_DESCRIPTION,
    create_module,
)
from tests.sql._support.create_from_sql_protocol import (
    CatalogConnection as FakeDbapiConnection,
)
from tests.sql._support.create_from_sql_protocol import (
    install_fake_type_registration,
)
from tests.sql._support.create_script_protocol import ScriptClickHouseClient, ScriptConnection

ddl_api = importlib.import_module("analytics_toolkit.sql.ddl.api")
SCRIPT = "CREATE TEMP TABLE prepared AS SELECT 7 AS id; SELECT id FROM prepared"


@pytest.fixture
def script_connections(monkeypatch):
    install_fake_type_registration(monkeypatch)
    get_config = create_module.get_connection_config

    def configured(key):
        return replace(get_config(key), transfer_staging_schema="sandbox")

    monkeypatch.setattr(create_module, "get_connection_config", configured)
    monkeypatch.setattr(source_script, "get_connection_config", configured)

    def operation_config(key):
        return create_module.get_connection_config(key)

    for module_name in (
        "analytics_toolkit.sql.dml.query_input",
        "analytics_toolkit.sql.dml.transfer.flow.adaptive_input",
    ):
        monkeypatch.setattr(
            importlib.import_module(module_name),
            "get_connection_config",
            operation_config,
        )
    connections = []
    states = {}

    def connect(key):
        connection = (
            ScriptClickHouseClient(states.get(key))
            if key == "ch"
            else ScriptConnection(
                description=SOURCE_DESCRIPTION,
                insert_rowcount=7,
                state=states.get(key),
                alias=key,
            )
        )
        states[key] = connection.state if key == "ch" else connection.catalog_state
        connections.append((key, connection))
        return connection

    monkeypatch.setattr(create_module, "get_sql_connection", connect)
    monkeypatch.setattr(create_module, "table_exists", lambda *_args: False)
    monkeypatch.setattr(
        ddl_api, "_handle_existing_create_target", lambda **_kw: ddl_api._CREATE_CONTINUE
    )
    return connections


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("insert_data", [False, True])
def test_script_prepares_once_and_uses_one_source_result(
    monkeypatch, script_connections, backend, insert_data
):
    inspections = []
    inserts = []

    def inspect(source_backend, connection, query):
        inspections.append((source_backend, connection, query))
        assert connection is script_connections[0][1]
        if source_backend == "gp":
            assert connection.commit_calls == int(insert_data)
        return [SourceColumn(name="id", native_type="INTEGER")]

    monkeypatch.setattr(create_module, "inspect_source_query_schema", inspect)
    monkeypatch.setattr(
        create_module,
        "insert_from_query",
        lambda *args, **_kw: inserts.append(args) or 7,
    )
    monkeypatch.setattr(
        create_module,
        "transfer_table",
        lambda **_kw: pytest.fail("same source must insert directly"),
    )
    result = sql.create_table(
        backend,
        "sandbox.created",
        sql=SCRIPT,
        insert_data=insert_data,
        ch_only_shard=backend == "ch",
        return_metadata=True,
        retry_cnt=1,
    )
    source = script_connections[0][1]
    commands = source.commands if backend == "ch" else source.executed
    assert sum("CREATE TEMP TABLE prepared" in command for command in commands) == 1
    assert len(inspections) == 1
    assert result.rows == (7 if insert_data else None)
    assert result.metadata.source_stage_count == int(insert_data)
    if insert_data:
        materializations = [
            command for command in commands if "AS SELECT id FROM prepared" in command
        ]
        assert len(materializations) == 1
        stage = result.metadata.stage_tables[0]
        assert inspections[0][2] == f"SELECT * FROM {stage}"
        assert inserts[0][3] == inspections[0][2]
        cleanup = script_connections[-1][1]
        drops = cleanup.commands if backend == "ch" else cleanup.executed
        assert any(command == f"DROP TABLE IF EXISTS {stage}" for command in drops)
    else:
        assert inspections[0][2] == "SELECT id FROM prepared"
        assert not inserts
        assert not any("__stage__" in command for command in commands)
    assert all(connection.close_calls == 1 for _, connection in script_connections)


def test_cross_database_transfer_reads_stage_without_replaying_setup(
    monkeypatch, script_connections
):
    calls = []
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn(name="id", native_type="INTEGER")],
    )
    monkeypatch.setattr(create_module, "transfer_table", lambda **kwargs: calls.append(kwargs) or 7)
    result = sql.create_table(
        "trino", "sandbox.created", source_db="gp", sql=SCRIPT, insert_data=True, retry_cnt=1
    )
    assert result == 7
    assert len(calls) == 1
    assert calls[0]["from_db"] == "gp"
    assert calls[0]["from_sql"].startswith("SELECT * FROM sandbox.")
    assert "prepared" not in calls[0]["from_sql"]
    assert calls[0]["ignore_source_staging"] is True
    assert calls[0]["retry_cnt"] == calls[0]["full_retry_cnt"] == 1
    assert script_connections[0][1].close_calls == 1


@pytest.mark.parametrize("backend", ["gp", "trino", "ch"])
@pytest.mark.parametrize("mode", ["dry_run", "return_sql"])
@pytest.mark.parametrize("insert_data", [False, True])
def test_script_plans_are_ordered_and_open_no_connections(
    monkeypatch, script_connections, backend, mode, insert_data
):
    monkeypatch.setattr(
        create_module, "get_sql_connection", lambda *_: pytest.fail("plan opened a connection")
    )
    plan = sql.create_table(
        backend,
        "sandbox.created",
        sql=SCRIPT,
        insert_data=insert_data,
        drop_if_exists=True,
        query_label="script-plan",
        **{mode: True},
    )
    phases = [statement.phase for statement in plan.statements]
    assert phases[0] == "setup"
    if backend == "ch":
        assert plan.options["pending_existing_layout_verification"] is True
        assert plan.options["pending_resource_scope_resolution"] is True
        assert plan.metadata.statement_count is None
        assert "drop_target" not in phases
        assert all(
            "DROP TABLE" not in statement.sql
            for statement in plan.statements
            if statement.phase != "cleanup_source_stage"
        )
    else:
        assert phases.index("inspect_source_schema") < phases.index("drop_target")
    assert phases.index("inspect_source_schema") < phases.index("create_target")
    assert phases.count("setup") == 1
    assert phases.count("inspect_source_schema") == 1
    assert phases.count("create_target") == 1
    if insert_data:
        assert phases[1] == "materialize_source"
        assert phases[-1] == "cleanup_source_stage"
        assert "insert_data" in phases
        assert phases.count("insert_data") == 1
        assert phases.index("create_target") < phases.index("insert_data")
        assert (
            sum("AS SELECT id FROM prepared" in statement.sql for statement in plan.statements) == 1
        )
    else:
        assert "materialize_source" not in phases
        assert "insert_data" not in phases
    assert all("script-plan" in statement.sql for statement in plan.statements)
    assert not script_connections


@pytest.mark.parametrize("query", ["", "-- only a comment", "; ;", "SELECT 1; DELETE FROM x"])
def test_invalid_scripts_fail_before_setup(monkeypatch, query):
    monkeypatch.setattr(
        create_module, "get_sql_connection", lambda *_: pytest.fail("opened connection")
    )
    with pytest.raises(create_module.InvalidSqlInputError):
        create_module.create_table_from_sql("gp", "sandbox.created", query, insert_data=False)


def test_script_parser_handles_ctes_comments_and_quoted_semicolons():
    setup, query = source_script.normalize_source_script(
        "CREATE TEMP TABLE prepared AS SELECT ';' AS value; "
        "WITH selected_rows AS (SELECT * FROM prepared) SELECT * FROM selected_rows; -- finished"
    )
    assert setup == ("CREATE TEMP TABLE prepared AS SELECT ';' AS value",)
    assert query.startswith("WITH selected_rows AS")


def test_missing_staging_schema_fails_before_setup(monkeypatch):
    monkeypatch.setattr(
        create_module, "get_sql_connection", lambda *_: pytest.fail("opened connection")
    )
    with pytest.raises(create_module.InvalidSqlInputError, match="transfer_staging_schema"):
        create_module.create_table_from_sql("gp", "sandbox.created", SCRIPT)


def test_generated_string_rejects_scripts_before_connections(monkeypatch):
    monkeypatch.setattr(ddl_api, "get_sql_connection", lambda *_: pytest.fail("opened connection"))
    with pytest.raises(create_module.InvalidSqlInputError, match="only_generate_sql"):
        sql.create_table("gp", "sandbox.created", sql=SCRIPT, only_generate_sql=True)


def test_existing_target_skips_script(monkeypatch):
    connection = FakeDbapiConnection()
    monkeypatch.setattr(create_module, "get_sql_connection", lambda *_: connection)
    monkeypatch.setattr(create_module, "table_exists", lambda *_: True)
    assert sql.create_table("gp", "sandbox.created", sql=SCRIPT, if_not_exists=True) is None
    assert connection.executed == []
    assert connection.close_calls == 1


@pytest.mark.parametrize("failure_phase", ["setup", "materialize", "inspect", "insert"])
def test_script_failure_retries_whole_attempt_and_cleans_stages(
    monkeypatch, script_connections, failure_phase
):
    counts = {"setup": 0, "materialize": 0, "inspect": 0, "insert": 0}

    def fail_once(phase):
        counts[phase] += 1
        if phase == failure_phase and counts[phase] == 1:
            msg = f"failed {phase}"
            raise RuntimeError(msg)

    execute_setup = source_script.execute_source_setup
    execute_materialization = source_script.execute_transfer_materialization

    def setup(*args, **kwargs):
        fail_once("setup")
        return execute_setup(*args, **kwargs)

    def materialize(*args, **kwargs):
        execute_materialization(*args, **kwargs)
        fail_once("materialize")

    def inspect(*_args):
        fail_once("inspect")
        return [SourceColumn(name="id", native_type="INTEGER")]

    def insert(*_args, **_kwargs):
        fail_once("insert")
        return 7

    monkeypatch.setattr(source_script, "execute_source_setup", setup)
    monkeypatch.setattr(source_script, "execute_transfer_materialization", materialize)
    monkeypatch.setattr(create_module, "inspect_source_query_schema", inspect)
    monkeypatch.setattr(create_module, "insert_from_query", insert)
    result = sql.create_table(
        "gp", "sandbox.created", sql=SCRIPT, insert_data=True, retry_cnt=2, timeout_increment=0
    )
    assert result == 7
    assert counts["setup"] == 2
    commands = [command for _, connection in script_connections for command in connection.executed]
    created_stages = [
        command.split()[2] for command in commands if "AS SELECT id FROM prepared" in command
    ]
    assert len(set(created_stages)) == len(created_stages)
    for index, stage in enumerate(created_stages):
        if failure_phase == "materialize" and index == 0:
            assert f"DROP TABLE IF EXISTS {stage}" not in commands
        else:
            assert f"DROP TABLE IF EXISTS {stage}" in commands
    assert all(connection.close_calls == 1 for _, connection in script_connections)


def test_stage_cleanup_failure_stops_retries(monkeypatch, script_connections):
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn(name="id", native_type="INTEGER")],
    )
    monkeypatch.setattr(create_module, "insert_from_query", lambda *_a, **_kw: 7)

    def fail_cleanup(*_args):
        msg = "stage cleanup unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(source_script, "_cleanup_stage", fail_cleanup)
    with pytest.raises(RuntimeError, match="stage cleanup unavailable"):
        sql.create_table(
            "gp", "sandbox.created", sql=SCRIPT, insert_data=True, retry_cnt=3, timeout_increment=0
        )
    assert len(script_connections) == 1


def test_routed_clickhouse_materializes_once_after_readiness(monkeypatch, script_connections):
    config = source_script.get_connection_config("ch")
    config = replace(config, cluster_routing=ChClusterRouting("core", "rand()"))
    original_config = create_module.get_connection_config
    monkeypatch.setattr(
        create_module,
        "get_connection_config",
        lambda key: config if key == "ch" else original_config(key),
    )
    monkeypatch.setattr(source_script, "get_connection_config", create_module.get_connection_config)
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn(name="id", native_type="Int32")],
    )
    monkeypatch.setattr(create_module, "transfer_table", lambda **_kw: 7)
    events = []
    materialize = source_script.execute_transfer_materialization

    def execute(*args):
        events.append(args[-1])
        return materialize(*args)

    def ready(_self, _connection, _stage, **kwargs):
        assert kwargs["ch_creation_policy"].shard_on_cluster == "core"
        events.append("ready")

    monkeypatch.setattr(source_script, "execute_transfer_materialization", execute)
    monkeypatch.setattr(type(get_backend_adapter("ch")), "after_create_table", ready)
    script = "SET max_threads = 2; SELECT id FROM sandbox.prepared"
    result = sql.create_table(
        "trino",
        "sandbox.created",
        source_db="ch",
        sql=script,
        insert_data=True,
        retry_cnt=1,
    )
    assert result == 7
    assert len(events) == 3
    assert "EMPTY AS SELECT id FROM sandbox.prepared" in events[0]
    assert "ON CLUSTER core" in events[0].replace("'", "")
    assert events[1] == "ready"
    assert events[2].startswith("INSERT INTO ")
    routed_insert = route_sql(events[2], routing=config.cluster_routing, database="sandbox")
    assert "INSERT INTO FUNCTION" in routed_insert
    assert "clusterAllReplicas" in routed_insert
    cleanup = script_connections[-1][1]
    assert any("ON CLUSTER core" in command.replace("'", "") for command in cleanup.commands)
    plan = sql.create_table(
        "trino",
        "sandbox.created",
        source_db="ch",
        sql=script,
        insert_data=True,
        dry_run=True,
    )
    phases = [step.phase for step in plan.statements]
    assert phases[:4] == [
        "setup",
        "materialize_source",
        "populate_source_stage",
        "inspect_source_schema",
    ]
    assert plan.options["source_stage_wait_policy"] in {"wait_shard", "wait_all"}


def test_stage_name_collision_never_drops_existing_table(monkeypatch, script_connections):
    monkeypatch.setattr(create_module, "table_exists", lambda *_args: True)
    with pytest.raises(RuntimeError, match="collision"):
        sql.create_table(
            "gp",
            "sandbox.created",
            sql=SCRIPT,
            insert_data=True,
            retry_cnt=1,
        )
    assert len(script_connections) == 1
    source = script_connections[0][1]
    assert source.rollback_calls == 1
    assert not any(command.startswith("DROP TABLE") for command in source.executed)


def test_script_input_requires_text():
    with pytest.raises(TypeError, match="string"):
        source_script.normalize_source_script(None)


def test_schema_only_script_preserves_none_return(monkeypatch, script_connections):
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn(name="id", native_type="INTEGER")],
    )
    assert sql.create_table("gp", "sandbox.created", sql=SCRIPT) is None


def test_legacy_single_query_normalizer_stays_strict():
    for query in ("", "SELECT 1; SELECT 2"):
        with pytest.raises(create_module.InvalidSqlInputError):
            create_module._normalize_single_query(query)


def test_failed_source_open_does_not_try_to_rollback_missing_connection(monkeypatch):
    def fail_open(_key):
        message = "source unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(create_module, "get_sql_connection", fail_open)
    with pytest.raises(RuntimeError, match="source unavailable"):
        create_module.create_table_from_sql("gp", "sandbox.created", "SELECT 1", retry_cnt=1)


def test_existing_options_positional_arguments_remain_compatible():
    options = create_module.CreateTableFromSqlOptions(
        "gp", "gp", "gp", "gp", "sandbox.created", "SELECT 1", {"id": "INTEGER"}, False
    )
    assert options.table_schema == {"id": "INTEGER"}
    assert options.insert_data is False
    assert options.setup_sqls == ()


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_preparation_failure_cleanup_preserves_error(
    monkeypatch, script_connections, interrupt, cleanup_fails
):
    failure = KeyboardInterrupt() if interrupt else RuntimeError("source materialization failed")
    cleaned = []

    def fail_materialize(_options, _connection, stage, _sql, **kwargs):
        kwargs["on_resource_created"](stage)
        raise failure

    def cleanup(_options, stage, _attempt):
        cleaned.append(stage)
        if cleanup_fails:
            message = "source stage cleanup failed"
            raise RuntimeError(message)

    monkeypatch.setattr(source_script, "_materialize_source", fail_materialize)
    monkeypatch.setattr(source_script, "_cleanup_stage", cleanup)
    with pytest.raises(KeyboardInterrupt if interrupt else RuntimeError) as caught:
        sql.create_table(
            "gp",
            "sandbox.created",
            sql=SCRIPT,
            insert_data=True,
            retry_cnt=2 if cleanup_fails else 1,
            timeout_increment=0,
        )
    assert len(cleaned) == 1
    assert script_connections[0][1].rollback_calls == 1
    assert script_connections[0][1].close_calls == 1
    assert caught.value is failure


@pytest.mark.parametrize("lost_ack", [False, True])
def test_source_create_without_ack_never_drops_candidate(monkeypatch, script_connections, lost_ack):
    failure = RuntimeError("lost source acknowledgement" if lost_ack else "source collision")
    original = source_script.execute_transfer_materialization

    def materialize(*args, **kwargs):
        if lost_ack:
            original(*args, **kwargs)
        raise failure

    monkeypatch.setattr(source_script, "execute_transfer_materialization", materialize)
    monkeypatch.setattr(
        source_script, "_cleanup_stage", lambda *_: pytest.fail("unowned candidate cleanup")
    )
    with pytest.raises(RuntimeError) as raised:
        sql.create_table("gp", "sandbox.created", sql=SCRIPT, insert_data=True, retry_cnt=1)
    assert raised.value is failure
    assert failure.analytics_toolkit_sql_unacknowledged_source_stage.startswith("sandbox.")
    assert all(connection.close_calls == 1 for _, connection in script_connections)


def test_unsafe_target_outcome_retains_owned_source_checkpoint(monkeypatch, script_connections):
    failure = RuntimeError("target acknowledgement lost")

    def unsafe(**kwargs):
        kwargs["source_connection"].close()
        return create_module._UnsafeAttemptFailure(failure, kwargs["attempt"])

    monkeypatch.setattr(create_module, "_execute_generic_create_table_from_sql_attempt", unsafe)
    monkeypatch.setattr(
        source_script, "_cleanup_stage", lambda *_: pytest.fail("required checkpoint deleted")
    )
    with pytest.raises(RuntimeError) as raised:
        sql.create_table(
            "gp", "sandbox.created", sql=SCRIPT, insert_data=True, retry_cnt=3, timeout_increment=0
        )
    assert raised.value is failure
    assert failure.analytics_toolkit_sql_retained_source_stage.startswith("sandbox.")
    assert len(script_connections) == 1


def test_source_cleanup_failure_preserves_primary_error(monkeypatch, script_connections):
    failure = RuntimeError("source inspection failed")

    def inspect(*_args):
        raise failure

    def cleanup(*_args):
        message = "cleanup disconnected"
        raise OSError(message)

    monkeypatch.setattr(create_module, "inspect_source_query_schema", inspect)
    monkeypatch.setattr(source_script, "_cleanup_stage", cleanup)
    with pytest.raises(RuntimeError) as raised:
        sql.create_table(
            "gp", "sandbox.created", sql=SCRIPT, insert_data=True, retry_cnt=3, timeout_increment=0
        )
    assert raised.value is failure
    assert failure.analytics_toolkit_sql_cleanup_error == "cleanup disconnected"
    assert len(script_connections) == 1


def test_source_ack_before_readiness_allows_only_owned_stage_cleanup(
    monkeypatch, script_connections
):
    config = replace(
        source_script.get_connection_config("ch"),
        cluster_routing=ChClusterRouting("core", "rand()"),
    )
    original_config = create_module.get_connection_config
    monkeypatch.setattr(
        create_module,
        "get_connection_config",
        lambda key: config if key == "ch" else original_config(key),
    )
    monkeypatch.setattr(source_script, "get_connection_config", create_module.get_connection_config)
    failure = RuntimeError("source readiness unavailable")
    cleanups = []

    def ready(*_args, **_kwargs):
        raise failure

    def cleanup(_backend, _connection, stage, **kwargs):
        cleanups.append(stage)
        assert kwargs["ch_creation_policy"].create_distributed_pair is False

    monkeypatch.setattr(type(get_backend_adapter("ch")), "after_create_table", ready)
    monkeypatch.setattr(source_script, "cleanup_stage_table", cleanup)
    with pytest.raises(RuntimeError) as raised:
        sql.create_table(
            "trino",
            "sandbox.created",
            source_db="ch",
            sql="SET max_threads = 2; SELECT id FROM sandbox.prepared",
            insert_data=True,
            retry_cnt=1,
        )
    assert raised.value is failure
    assert len(cleanups) == 1
    assert "__source" in cleanups[0]
    assert not any(
        command.startswith("INSERT INTO ") for command in script_connections[0][1].commands
    )


def test_certified_script_retry_reuses_payload_without_source_setup(
    monkeypatch, script_connections
):
    calls = []

    def execute(**kwargs):
        options = kwargs["options"]
        calls.append(options.source_sql)
        if len(calls) == 1:
            kwargs["source_connection"].close()
            options.input_resolution["prepared_query_input"] = SimpleNamespace(
                frozen=True,
                mutation_started=False,
                publication_succeeded=False,
                options=SimpleNamespace(
                    source_sql=options.source_sql,
                    input_resolution=options.input_resolution["transfer_input_resolution"],
                ),
            )
            message = "safe target failure after source certification"
            raise RuntimeError(message)
        assert kwargs.get("source_connection") is None
        assert options.setup_sqls == ()
        options.input_resolution.pop("prepared_query_input")
        return 7

    monkeypatch.setattr(create_module, "_execute_generic_create_table_from_sql_attempt", execute)
    assert (
        sql.create_table(
            "gp", "sandbox.created", sql=SCRIPT, insert_data=True, retry_cnt=2, timeout_increment=0
        )
        == 7
    )
    assert calls[0] == calls[1]
    commands = [command for _, connection in script_connections for command in connection.executed]
    assert sum("CREATE TEMP TABLE prepared" in command for command in commands) == 1
    assert sum("AS SELECT id FROM prepared" in command for command in commands) == 1


def test_existing_script_target_skip_preserves_metadata_return(monkeypatch, script_connections):
    monkeypatch.setattr(create_module, "table_exists", lambda *_: True)
    result = sql.create_table(
        "gp",
        "sandbox.created",
        sql=SCRIPT,
        if_not_exists=True,
        return_metadata=True,
    )
    assert result.rows is None
    assert result.metadata.statement_count == 0
    assert result.metadata.retry_attempts == 1
    assert script_connections[0][1].executed == []


def test_schema_only_unsafe_target_outcome_has_no_generated_source_checkpoint(
    monkeypatch, script_connections
):
    message = "schema target acknowledgement lost"
    failure = RuntimeError(message)

    def unsafe(**kwargs):
        kwargs["source_connection"].close()
        return create_module._UnsafeAttemptFailure(failure, kwargs["attempt"])

    monkeypatch.setattr(create_module, "_execute_generic_create_table_from_sql_attempt", unsafe)
    with pytest.raises(RuntimeError) as raised:
        sql.create_table("gp", "sandbox.created", sql=SCRIPT, retry_cnt=1)
    assert raised.value is failure
    assert not hasattr(failure, "analytics_toolkit_sql_retained_source_stage")


def test_source_materialization_keeps_optional_ack_callback_compatible():
    connection = ScriptConnection(description=SOURCE_DESCRIPTION)
    options = create_module.CreateTableFromSqlOptions(
        "gp", "gp", "gp", "gp", "sandbox.created", "SELECT 7 AS id"
    )
    source_script._materialize_source(options, connection, "sandbox.source_stage", "SELECT 7 AS id")
    assert any("CREATE TABLE sandbox.source_stage" in command for command in connection.executed)
    assert connection.commit_calls == 1


def test_if_not_exists_with_absent_target_executes_schema_script(monkeypatch, script_connections):
    monkeypatch.setattr(
        create_module,
        "inspect_source_query_schema",
        lambda *_args: [SourceColumn(name="id", native_type="INTEGER")],
    )
    assert sql.create_table("gp", "sandbox.created", sql=SCRIPT, if_not_exists=True) is None
    assert script_connections[0][1].executed == []
    assert any(
        "CREATE TEMP TABLE prepared" in command for command in script_connections[1][1].executed
    )
    assert all(connection.close_calls == 1 for _, connection in script_connections)


def test_query_replace_flag_rejects_non_clickhouse_before_connections(monkeypatch):
    monkeypatch.setattr(
        create_module, "get_sql_connection", lambda *_a: pytest.fail("opened connection")
    )
    with pytest.raises(ValueError, match="requires a ClickHouse target"):
        sql.create_table("gp", "sandbox.created", sql="SELECT 1 AS id", ch_replace_table=True)

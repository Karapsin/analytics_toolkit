from __future__ import annotations

from tests.sql._support.connection_config import (
    CreateSqlTableOptions,
    InvalidSqlInputError,
    SqlOperationMetadata,
    SqlTableReadinessError,
    _fake_drop_target_sqls,
    create_sql_table_module,
    operation_runner_module,
    pd,
    pytest,
    replace,
    target_replace_module,
    transfer_schema_module,
    types,
)


def test_create_table_dataframe_and_name_validation_edges() -> None:
    with pytest.raises(InvalidSqlInputError, match="Exactly one schema source"):
        create_sql_table_module._resolve_create_dataframe_and_schema(
            df=None,
            table_schema=None,
        )
    with pytest.raises(TypeError, match="df must be a pandas DataFrame"):
        create_sql_table_module._resolve_create_dataframe_and_schema(
            df=[],
            table_schema=None,
        )
    with pytest.raises(InvalidSqlInputError, match="Exactly one schema source"):
        create_sql_table_module._resolve_create_dataframe_and_schema(
            df=pd.DataFrame({"id": [1]}),
            table_schema={"id": "BIGINT"},
        )
    with pytest.raises(ValueError, match="table_name must not be empty"):
        create_sql_table_module.create_table(
            "gp",
            " ",
            pd.DataFrame({"id": [1]}),
            only_generate_sql=True,
        )


def test_create_table_distinguishes_ddl_timeout_from_readiness_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddl_timeout = TimeoutError("DDL submission outcome is unknown")
    readiness_timeout = TimeoutError("visible on 20/22 hosts")

    class FakeAdapter:
        def __init__(self, *, fail_during_readiness: bool) -> None:
            self.fail_during_readiness = fail_during_readiness

        def expected_create_table_column_types(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, str]:
            return {"id": "Int64"}

        def execute_commands(self, _connection: object, _sqls: list[str]) -> None:
            if not self.fail_during_readiness:
                raise ddl_timeout

        def after_create_table(
            self,
            _connection: object,
            _table_name: str,
            **_kwargs: object,
        ) -> None:
            if self.fail_during_readiness:
                raise readiness_timeout

    options = CreateSqlTableOptions(
        connection_key="ch_alias",
        backend="ch",
        table_name="scratch.target",
        df=pd.DataFrame({"id": [1]}),
    )

    monkeypatch.setattr(
        create_sql_table_module,
        "get_backend_adapter",
        lambda _backend: FakeAdapter(fail_during_readiness=True),
    )
    with pytest.raises(SqlTableReadinessError, match="20/22") as readiness_error:
        create_sql_table_module._execute_create_sql_table(
            options=options,
            connection=object(),
            create_sqls=["CREATE TABLE scratch.target (id Int64)"],
            metadata=SqlOperationMetadata(),
            retry_attempt=None,
        )
    assert isinstance(readiness_error.value.__cause__, TimeoutError)

    monkeypatch.setattr(
        create_sql_table_module,
        "get_backend_adapter",
        lambda _backend: FakeAdapter(fail_during_readiness=False),
    )
    with pytest.raises(TimeoutError, match="outcome is unknown") as ddl_error:
        create_sql_table_module._execute_create_sql_table(
            options=options,
            connection=object(),
            create_sqls=["CREATE TABLE scratch.target (id Int64)"],
            metadata=SqlOperationMetadata(),
            retry_attempt=None,
        )
    assert not isinstance(ddl_error.value, SqlTableReadinessError)


@pytest.mark.parametrize(
    "schema_source",
    [
        {"df": pd.DataFrame({"id": [1]})},
        {"table_schema": {"id": "BIGINT"}},
    ],
)
def test_create_table_drop_target_runs_before_every_create_attempt(
    monkeypatch: pytest.MonkeyPatch,
    schema_source: dict[str, object],
) -> None:
    events: list[tuple[str, int, bool]] = []
    monkeypatch.setattr(
        create_sql_table_module,
        "get_connection_config",
        lambda key: types.SimpleNamespace(connection_key=key, backend="gp"),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_sql_table_sqls",
        lambda options, option_owner: ["CREATE TABLE mart.target (id BIGINT)"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "build_drop_target_sqls",
        lambda options: ["DROP TABLE IF EXISTS mart.target"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "drop_existing_target",
        lambda **kwargs: events.append(
            (
                "drop",
                kwargs["retry_attempt"],
                kwargs["options"].drop_target_if_exists,
            )
        ),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_execute_create_sql_table",
        lambda **kwargs: events.append(
            (
                "create",
                kwargs["retry_attempt"],
                kwargs["options"].drop_target_if_exists,
            )
        ),
    )

    def fake_run_connection_operation(**kwargs: object) -> None:
        kwargs["operation"]({"connection": object()}, 1)
        context = kwargs["context_factory"](2)
        assert context.phase == "replace_target"
        assert context.sql_preview == "DROP TABLE IF EXISTS mart.target"
        kwargs["operation"]({"connection": object()}, 2)

    monkeypatch.setattr(
        create_sql_table_module,
        "run_connection_operation",
        fake_run_connection_operation,
    )

    result = create_sql_table_module.create_table(
        "gp_alias",
        "mart.target",
        drop_if_exists=True,
        return_metadata=True,
        **schema_source,
    )

    assert events == [
        ("drop", 1, True),
        ("create", 1, True),
        ("drop", 2, True),
        ("create", 2, True),
    ]
    assert result.metadata.statement_count == 2
    assert result.plan.sqls == [
        "DROP TABLE IF EXISTS mart.target",
        "CREATE TABLE mart.target (id BIGINT)",
    ]
    assert [statement.phase for statement in result.plan.statements] == [
        "drop_target",
        "create_table",
    ]
    assert result.plan.options["drop_target_if_exists"] is True


def test_create_table_execution_returns_metadata_and_builds_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        create_sql_table_module,
        "get_connection_config",
        lambda key: types.SimpleNamespace(connection_key=key, backend="gp"),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_sql_table_sqls",
        lambda options, option_owner: ["create table mart.target (id bigint)"],
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_execute_create_sql_table",
        lambda **kwargs: events.append(("execute", kwargs)),
    )

    def fake_run_connection_operation(**kwargs: object) -> None:
        context = kwargs["context_factory"](2)
        events.append(("context", context))
        kwargs["operation"]({"connection": object()}, 2)

    monkeypatch.setattr(
        create_sql_table_module,
        "run_connection_operation",
        fake_run_connection_operation,
    )

    result = create_sql_table_module.create_table(
        "gp_alias",
        "mart.target",
        pd.DataFrame({"id": [1]}),
        return_metadata=True,
    )

    assert result.metadata.statement_count == 1
    assert result.plan.operation == "create_table"
    assert result.plan.sqls == ["create table mart.target (id bigint)"]
    context = next(event[1] for event in events if event[0] == "context")
    assert context.retry_attempt == 2
    execution = next(event[1] for event in events if event[0] == "execute")
    assert execution["retry_attempt"] == 2


def test_create_table_from_sql_dry_run_accepts_scalar_distribution_key() -> None:
    plan = create_sql_table_module.create_table(
        db_key="gp",
        table_name="schema.target",
        sql="select description from source_table",
        gp_distributed_by_key="description",
        return_sql=True,
    )

    assert plan.operation == "create_table_from_sql"
    assert plan.options["gp_distributed_by_key"] == ["description"]


def test_create_table_from_sql_only_generate_inspects_and_maps_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class FakeConnection:
        def close(self) -> None:
            events.append("close")

    class FakeTargetAdapter:
        def normalize_ch_columns_or_expression(
            self,
            value: object,
            option_name: str,
        ) -> object:
            events.append(("normalize_columns", option_name, value))
            return value

        def normalize_ch_string(self, value: str, option_name: str) -> str:
            events.append(("normalize_string", option_name, value))
            return value

        def validate_gp_distributed_by_key_option(
            self,
            value: object,
            *,
            option_owner: str,
        ) -> None:
            events.append(("validate_gp", value, option_owner))

        normalize_gp_partitions_option = staticmethod(lambda value, **kwargs: value)

        def validate_ch_create_table_options(self, **kwargs: object) -> None:
            events.append(("validate_ch_options", kwargs))

        def validate_ch_columns_in_columns(
            self,
            value: object,
            columns: list[str],
            option_name: str,
            *,
            data_name: str,
        ) -> None:
            events.append(("validate_ch_columns", value, columns, option_name, data_name))

        def build_create_from_sql_target_create_kwargs(
            self,
            **kwargs: object,
        ) -> dict[str, object]:
            events.append(("create_kwargs", kwargs))
            assert kwargs["drop_target_if_exists"] is True
            return {"ch_distributed_table": False}

        build_drop_target_sqls = staticmethod(_fake_drop_target_sqls)

    def fake_config(key: str) -> types.SimpleNamespace:
        backend = "trino" if key == "source_alias" else "gp"
        return types.SimpleNamespace(connection_key=key, backend=backend)

    def fake_inspect(
        backend: str,
        connection: object,
        query: str,
    ) -> list[types.SimpleNamespace]:
        events.append(("inspect", backend, connection, query))
        return [
            types.SimpleNamespace(name="id"),
            types.SimpleNamespace(name="amount"),
        ]

    monkeypatch.setattr(create_sql_table_module, "get_connection_config", fake_config)
    monkeypatch.setattr(
        create_sql_table_module,
        "get_backend_adapter",
        lambda backend: FakeTargetAdapter(),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "get_sql_connection",
        lambda key: FakeConnection(),
    )
    monkeypatch.setattr(
        transfer_schema_module,
        "inspect_source_query_schema",
        fake_inspect,
    )
    monkeypatch.setattr(
        transfer_schema_module,
        "map_source_schema_to_target",
        lambda source_schema, backend, **_kwargs: {
            column.name: "BIGINT" for column in source_schema
        },
    )
    monkeypatch.setattr(
        operation_runner_module,
        "run_retrying_operation",
        lambda **kwargs: kwargs["operation"](1),
    )
    monkeypatch.setattr(
        create_sql_table_module,
        "_build_create_table_sqls",
        lambda *args, **kwargs: ["create first;", "create second;"],
    )

    generated = create_sql_table_module.create_table(
        db_key="target_alias",
        source_db="source_alias",
        table_name="mart.target",
        sql="select id, amount from source",
        gp_distributed_by_key="id",
        drop_if_exists=True,
        only_generate_sql=True,
        query_label="coverage-ddl",
    )

    assert generated == "drop target;\ncreate first;\ncreate second"
    assert "close" in events
    inspect_event = next(event for event in events if event[0] == "inspect")
    assert inspect_event[1] == "trino"
    assert "coverage-ddl" in inspect_event[3]


@pytest.mark.parametrize(
    "schema_source",
    [
        {"df": pd.DataFrame({"id": [1]})},
        {"table_schema": {"id": "BIGINT"}},
    ],
)
def test_create_table_only_generate_includes_drop_for_regular_schema_sources(
    schema_source: dict[str, object],
) -> None:
    generated = create_sql_table_module.create_table(
        "gp",
        "schema.target",
        drop_if_exists=True,
        only_generate_sql=True,
        **schema_source,
    )

    assert generated.startswith("DROP TABLE IF EXISTS schema.target;\nCREATE TABLE")


def test_create_table_sql_accepts_connection_alias() -> None:
    sql = create_sql_table_module.create_table(
        db_key="gp_sandbox",
        table_name="schema.target",
        df=pd.DataFrame({"id": [1], "value": ["x"]}),
        gp_distributed_by_key=["id"],
        only_generate_sql=True,
    )

    assert '"id" BIGINT' in sql
    assert 'DISTRIBUTED BY ("id")' in sql


def test_create_table_sql_accepts_scalar_distribution_key() -> None:
    sql = create_sql_table_module.create_table(
        db_key="gp",
        table_name="schema.target",
        df=pd.DataFrame({"description": ["x"], "value": [1]}),
        gp_distributed_by_key=" description ",
        only_generate_sql=True,
    )

    assert 'DISTRIBUTED BY ("description")' in sql


def test_create_table_sql_accepts_table_schema_override() -> None:
    gp_sql = create_sql_table_module.create_table(
        db_key="gp",
        table_name="schema.target",
        table_schema={"id": "TEXT", "amount": "NUMERIC(10, 2)"},
        only_generate_sql=True,
    )
    trino_sql = create_sql_table_module.create_table(
        db_key="trino",
        table_name="schema.target",
        table_schema={"id": "VARCHAR", "amount": "DECIMAL(10, 2)"},
        only_generate_sql=True,
    )
    ch_sqls = create_sql_table_module._build_create_table_sqls(
        backend="ch",
        table_name="schema.target",
        df=pd.DataFrame(columns=["id", "amount"]),
        table_schema={"id": "String", "amount": "Decimal(10, 2)"},
        ch_distributed_table=True,
    )

    assert '"id" TEXT' in gp_sql
    assert '"amount" NUMERIC(10, 2)' in gp_sql
    assert '"id" VARCHAR' in trino_sql
    assert '"amount" DECIMAL(10, 2)' in trino_sql
    assert any("`id` String" in sql for sql in ch_sqls)
    assert any("`amount` Decimal(10, 2)" in sql for sql in ch_sqls)


def test_create_table_sql_rejects_invalid_table_schema_type() -> None:
    with pytest.raises(TypeError, match="table_schema"):
        create_sql_table_module.create_table(
            db_key="gp",
            table_name="schema.target",
            table_schema=[("id", "BIGINT")],
            only_generate_sql=True,
        )


def test_create_table_sql_rejects_multiple_schema_sources() -> None:
    with pytest.raises(InvalidSqlInputError, match="Exactly one schema source"):
        create_sql_table_module.create_table(
            db_key="gp",
            table_name="schema.target",
            df=pd.DataFrame({"id": [1]}),
            table_schema={"id": "TEXT"},
            only_generate_sql=True,
        )


@pytest.mark.parametrize(
    ("table_schema", "match"),
    [
        ({"id": "BIGINT", "amount": " "}, "must not be empty"),
    ],
)
def test_create_table_sql_validates_table_schema(
    table_schema: dict[str, str],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        create_sql_table_module.create_table(
            db_key="gp",
            table_name="schema.target",
            table_schema=table_schema,
            only_generate_sql=True,
        )


def test_create_table_target_replace_helper_uses_backend_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeAdapter:
        def build_drop_target_sqls(
            self,
            table_name: str,
            **kwargs: object,
        ) -> list[str]:
            events.append(("build", table_name, kwargs))
            return ["DROP TARGET"]

        def prepare_existing_target_for_create_from_sql(
            self,
            connection: object,
            table_name: str,
            **kwargs: object,
        ) -> None:
            events.append(("drop", connection, table_name, kwargs))

    monkeypatch.setattr(
        target_replace_module,
        "get_backend_adapter",
        lambda backend: FakeAdapter(),
    )
    base_options = CreateSqlTableOptions(
        connection_key="gp_alias",
        backend="gp",
        table_name="mart.target",
        df=pd.DataFrame({"id": [1]}),
    )
    metadata = SqlOperationMetadata()

    assert target_replace_module.build_drop_target_sqls(base_options) == []
    target_replace_module.drop_existing_target(
        options=base_options,
        connection=object(),
        drop_sqls=[],
        metadata=metadata,
        retry_attempt=1,
    )
    assert events == []

    replace_options = replace(base_options, drop_target_if_exists=True)
    assert target_replace_module.build_drop_target_sqls(replace_options) == ["DROP TARGET"]
    connection = object()
    target_replace_module.drop_existing_target(
        options=replace_options,
        connection=connection,
        drop_sqls=["DROP TARGET"],
        metadata=metadata,
        retry_attempt=2,
    )

    assert events[0][0:2] == ("build", "mart.target")
    assert events[1][0:3] == ("drop", connection, "mart.target")
    assert events[1][3]["drop_target_if_exists"] is True
    assert events[1][3]["connection_key"] == "gp_alias"
    assert metadata.retry_attempts == 2
    assert metadata.operation_status == "success"

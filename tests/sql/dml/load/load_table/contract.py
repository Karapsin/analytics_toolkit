from __future__ import annotations

from tests.sql._support.load_table import (
    TEST_CH_SHARD_TABLE,
    TEST_CH_STAGE_TABLE,
    TEST_CH_TABLE,
    UUID,
    Decimal,
    FakeClickHouseClient,
    FakeDbapiConnection,
    SimpleNamespace,
    date,
    gp_insert_module,
    load_df_module,
    load_sql_table_module,
    pd,
    pytest,
    table_ops_module,
    trino_insert_module,
)


def test_batch_insert_sql_builders_preserve_backend_shapes() -> None:
    assert load_sql_table_module.build_gp_batch_insert_sql(
        "schema.stage_table",
        ["id", "value"],
        query_label="load-stage",
    ) == (
        "/* analytics_toolkit query_label=load-stage */\n"
        'INSERT INTO schema.stage_table ("id", "value") VALUES %s'
    )

    assert load_sql_table_module.build_trino_batch_insert_sql(
        "schema.stage_table",
        ["id", "value"],
        row_count=2,
    ) == ('INSERT INTO schema.stage_table ("id", "value") VALUES (?, ?), (?, ?)')


@pytest.mark.parametrize("write_mode", ["append", "upsert"])
def test_empty_existing_load_returns_metadata(write_mode: str) -> None:
    metadata = load_df_module.SqlOperationMetadata()
    result = load_df_module._handle_empty_dataframe_load(
        SimpleNamespace(
            append=write_mode == "append",
            write_mode=write_mode,
            destination_table="sandbox.target",
        ),
        load_df_module.LoadState(
            target_exists=True,
            original_target_exists=True,
        ),
        operation_metadata=metadata,
        return_metadata=True,
    )

    assert result.rows == 0
    assert result.metadata is metadata
    assert metadata.inserted_rows == 0
    assert metadata.affected_rows == 0


def test_finalize_stage_table_clickhouse_ensures_existing_pair_before_insert() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": [date(2024, 2, 1)],
            "users": [10],
        }
    )
    column_types = {
        "month_date": "Nullable(Date)",
        "users": "Nullable(Int64)",
    }

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=False,
        target_exists=True,
        sample_batch=batch,
        insert_column_types=column_types,
        partition_by=["month_date"],
        order_by=["month_date"],
        ch_cluster="core",
    )

    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE}" not in client.commands
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}")
        and "ON CLUSTER core" in command
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_TABLE}")
        and "ON CLUSTER core" in command
        for command in client.commands
    )
    assert client.commands[-1] == (
        f"INSERT INTO {TEST_CH_TABLE} (`month_date`, `users`) "
        f"SELECT CAST(`month_date` AS Nullable(Date)) AS `month_date`, "
        f"CAST(`users` AS Nullable(Int64)) AS `users` "
        f"FROM {TEST_CH_STAGE_TABLE}"
    )

    insert_index = client.commands.index(client.commands[-1])
    create_indices = [
        idx
        for idx, command in enumerate(client.commands)
        if command.startswith("CREATE TABLE IF NOT EXISTS")
    ]
    assert create_indices
    assert max(create_indices) < insert_index


def test_finalize_stage_table_clickhouse_only_shard_upsert_deletes_target() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="id",
        final_upsert_stage_table=f"{TEST_CH_TABLE}__final_stage",
        insert_column_types={"id": "UInt64", "score": "Int64"},
        ch_cluster="core",
        ch_only_shard=True,
    )

    drop_sql = next(sql for sql in client.commands if "DROP PARTITION" in sql)
    assert drop_sql == f"ALTER TABLE {TEST_CH_TABLE} DROP PARTITION 1"
    assert "ON CLUSTER" not in drop_sql


def test_finalize_stage_table_clickhouse_recreates_pair_and_inserts_target() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": [date(2024, 2, 1)],
            "min_month_use": [date(2024, 1, 1)],
            "users": [10],
        }
    )

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        partition_by=["month_date"],
        order_by=["month_date", "min_month_use"],
        ch_sharding_key="cityHash64(month_date, min_month_use)",
    )

    assert f"DROP TABLE IF EXISTS {TEST_CH_TABLE}" not in client.commands
    assert f"DROP TABLE IF EXISTS {TEST_CH_SHARD_TABLE}" not in client.commands
    assert any(
        command.startswith(f"CREATE TABLE IF NOT EXISTS {TEST_CH_SHARD_TABLE}__replace_")
        for command in client.commands
    )
    assert any(
        command.startswith(f"CREATE TABLE {TEST_CH_TABLE}__replace_") for command in client.commands
    )
    assert any(
        command.startswith(f"INSERT INTO {TEST_CH_SHARD_TABLE}__replace_")
        and f"FROM {TEST_CH_STAGE_TABLE}" in command
        for command in client.commands
    )
    assert any(
        command.startswith(f"RENAME TABLE {TEST_CH_SHARD_TABLE} TO ") for command in client.commands
    )
    assert any(
        f" TO {TEST_CH_SHARD_TABLE} ON CLUSTER '{{cluster}}'" in command
        for command in client.commands
    )
    assert not any(query.startswith("DESCRIBE TABLE ") for query in client.queries)


def test_finalize_stage_table_clickhouse_uses_explicit_types_and_casts_insert() -> None:
    client = FakeClickHouseClient()
    batch = pd.DataFrame(
        {
            "month_date": ["2024-02-01"],
            "users": ["10"],
        }
    )
    column_types = {
        "month_date": "Nullable(Date)",
        "users": "Nullable(Int64)",
    }

    table_ops_module.finalize_stage_table(
        connection_type="ch",
        connection=client,
        stage_table=TEST_CH_STAGE_TABLE,
        target_table=TEST_CH_TABLE,
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        target_column_types=column_types,
        insert_column_types=column_types,
        partition_by=["month_date"],
        order_by=["month_date"],
    )

    create_sql = "\n".join(client.commands)
    assert "`month_date` Nullable(Date)" in create_sql
    assert "`users` Nullable(Int64)" in create_sql
    insert_sql = next(command for command in client.commands if command.startswith("INSERT INTO"))
    assert insert_sql.startswith(f"INSERT INTO {TEST_CH_SHARD_TABLE}__replace_")
    assert insert_sql.endswith(
        " (`month_date`, `users`) "
        f"SELECT CAST(`month_date` AS Nullable(Date)) AS `month_date`, "
        f"CAST(`users` AS Nullable(Int64)) AS `users` "
        f"FROM {TEST_CH_STAGE_TABLE}"
    )


def test_finalize_stage_table_greenplum_upsert_deletes_then_inserts() -> None:
    connection = FakeDbapiConnection()
    batch = pd.DataFrame({"id": [1], "sub_id": [None], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="gp",
        connection=connection,
        stage_table="sandbox.target__stage",
        target_table="sandbox.target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id", "sub_id"],
        insert_column_types={"id": "BIGINT", "sub_id": "BIGINT", "score": "INTEGER"},
    )

    assert connection.executed[0] == (
        "DELETE FROM sandbox.target AS target_dst\n"
        "USING sandbox.target__stage AS stage_src\n"
        'WHERE (target_dst."id" = stage_src."id" '
        'OR (target_dst."id" IS NULL AND stage_src."id" IS NULL)) '
        'AND (target_dst."sub_id" = stage_src."sub_id" '
        'OR (target_dst."sub_id" IS NULL AND stage_src."sub_id" IS NULL))'
    )
    assert connection.executed[1].startswith(
        'INSERT INTO sandbox.target ("id", "sub_id", "score") '
    )


def test_finalize_stage_table_trino_replace_recreates_target() -> None:
    connection = FakeDbapiConnection()
    batch = pd.DataFrame(
        {
            "contact_id": ["1"],
            "first_game_dt": [date(2026, 1, 1)],
            "last_game_dt": [date(2026, 1, 2)],
        }
    )
    column_types = {
        "contact_id": "VARCHAR",
        "first_game_dt": "DATE",
        "last_game_dt": "DATE",
    }

    table_ops_module.finalize_stage_table(
        connection_type="trino",
        connection=connection,
        stage_table="iceberg.pa_core_stage.target__stage",
        target_table="iceberg.pa_core_sandbox.target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        target_column_types=column_types,
        insert_column_types=column_types,
    )

    assert connection.executed[0].startswith(
        "CREATE TABLE iceberg.pa_core_sandbox.target__replace_"
    )
    assert (
        '("contact_id" VARCHAR, "first_game_dt" DATE, "last_game_dt" DATE)'
        in connection.executed[0]
    )
    replacement = connection.executed[0].split("CREATE TABLE ", 1)[1].split(" ", 1)[0]
    assert connection.executed[1] == (
        f'INSERT INTO {replacement} ("contact_id", '
        '"first_game_dt", "last_game_dt") '
        'SELECT CAST("contact_id" AS VARCHAR) AS "contact_id", '
        'CAST("first_game_dt" AS DATE) AS "first_game_dt", '
        'CAST("last_game_dt" AS DATE) AS "last_game_dt" '
        "FROM iceberg.pa_core_stage.target__stage"
    )
    assert "ALTER TABLE iceberg.pa_core_sandbox.target RENAME TO " in connection.executed[4]
    assert connection.executed[5] == (
        f"ALTER TABLE {replacement} RENAME TO iceberg.pa_core_sandbox.target"
    )
    assert connection.executed[-1].startswith(
        "DROP TABLE IF EXISTS iceberg.pa_core_sandbox.target__backup_"
    )
    assert not any(
        sql == "DROP TABLE IF EXISTS iceberg.pa_core_sandbox.target" for sql in connection.executed
    )


def test_finalize_stage_table_trino_upsert_replaces_affected_partitions() -> None:
    connection = FakeDbapiConnection(rows=[("2026-06-24",)])
    batch = pd.DataFrame({"id": [1], "score": [10]})

    table_ops_module.finalize_stage_table(
        connection_type="trino",
        connection=connection,
        stage_table="sandbox.target__stage",
        target_table="sandbox.target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        write_mode="upsert",
        key_columns=["id"],
        upsert_partition_column="event_date",
        final_upsert_stage_table="sandbox.target__final_stage",
        trino_upsert_partition_drop_sql_template=(
            "ALTER TABLE {table} DROP PARTITION ({partition_column} = {partition_value})"
        ),
        insert_column_types={"id": "BIGINT", "score": "INTEGER"},
    )

    assert connection.executed[0] == ('SELECT DISTINCT "event_date" FROM sandbox.target__stage')
    assert connection.executed[1].startswith(
        'INSERT INTO sandbox.target__final_stage ("id", "score")\n'
        'SELECT target_dst."id", target_dst."score"\n'
        "FROM sandbox.target AS target_dst"
    )
    assert connection.executed[3] == (
        "ALTER TABLE sandbox.target DROP PARTITION (\"event_date\" = '2026-06-24')"
    )
    assert connection.executed[4].startswith('INSERT INTO sandbox.target ("id", "score") ')


def test_insert_gp_rows_can_change_page_size_between_calls(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured_calls: list[dict[str, object]] = []
    page_sizes = iter([2, 3, 10])

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql
        captured_calls.append(
            {
                "rows": list(rows),
                "page_size": page_size,
            }
        )

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    load_sql_table_module._insert_gp_rows(
        connection=connection,
        table_name="schema.stage_table",
        columns=["id"],
        rows=[(1,), (2,), (3,), (4,), (5,), (6,)],
        page_size_getter=lambda: next(page_sizes),
    )

    assert captured_calls == [
        {"rows": [(1,), (2,)], "page_size": 2},
        {"rows": [(3,), (4,), (5,)], "page_size": 3},
        {"rows": [(6,)], "page_size": 1},
    ]
    assert connection.commit_calls == 1


def test_insert_gp_rows_reports_per_page_success(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    page_successes: list[tuple[float, int]] = []
    progress_updates: list[int] = []
    perf_values = iter([1.0, 1.25, 2.0, 2.75])

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql, rows, page_size

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)
    monkeypatch.setattr(
        gp_insert_module.time,
        "perf_counter",
        lambda: next(perf_values),
    )

    load_sql_table_module._insert_gp_rows(
        connection=connection,
        table_name="schema.stage_table",
        columns=["id"],
        rows=[(1,), (2,), (3,)],
        gp_insert_chunk_size=2,
        on_progress=progress_updates.append,
        on_page_success=lambda duration, rows: page_successes.append((duration, rows)),
    )

    assert progress_updates == [2, 1]
    assert page_successes == [(0.25, 2), (0.75, 1)]
    assert connection.commit_calls == 1


def test_insert_rows_batch_clickhouse_uses_rows_and_column_type_names() -> None:
    client = FakeClickHouseClient()
    progress_updates: list[int] = []

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="ch",
        connection_ref={"connection": client},
        table_name="schema.stage_table",
        columns=["amount", "label"],
        rows=[(Decimal("1.20"), "ok"), (None, pd.NA)],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
        target_column_types={
            "amount": "Nullable(Decimal(10, 2))",
            "label": "Nullable(String)",
        },
        on_progress=progress_updates.append,
    )

    assert inserted_rows == 2
    assert progress_updates == [2]
    assert client.calls == [
        {
            "table": "schema.stage_table",
            "data": [(Decimal("1.2"), "ok"), (None, None)],
            "column_names": ["amount", "label"],
            "column_type_names": [
                "Nullable(Decimal(10, 2))",
                "Nullable(String)",
            ],
        }
    ]


def test_insert_rows_batch_gp_honors_insert_chunk_size(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured_calls: list[dict[str, object]] = []
    progress_updates: list[int] = []

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql
        captured_calls.append(
            {
                "rows": list(rows),
                "page_size": page_size,
            }
        )

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="gp",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["id"],
        rows=[(1,), (2,), (3,)],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
        gp_insert_chunk_size=2,
        on_progress=progress_updates.append,
    )

    assert inserted_rows == 3
    assert captured_calls == [
        {"rows": [(1,), (2,)], "page_size": 2},
        {"rows": [(3,)], "page_size": 1},
    ]
    assert progress_updates == [2, 1]
    assert connection.commit_calls == 1


def test_insert_rows_batch_gp_normalizes_uuid_values(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured_rows: list[tuple[object, ...]] = []
    uuid_value = UUID("f5d10b74-0409-4f31-bc7c-df82b8688f19")

    def fake_execute_values(cursor, sql, rows, page_size):
        del cursor, sql, page_size
        captured_rows.extend(rows)

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="gp",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["uuid_value"],
        rows=[(uuid_value,)],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 1
    assert captured_rows == [(str(uuid_value),)]


def test_insert_rows_batch_gp_uses_row_tuples_and_normalizes_nulls(monkeypatch) -> None:
    connection = FakeDbapiConnection()
    captured: dict[str, object] = {}

    def fake_execute_values(cursor, sql, rows, page_size):
        captured["sql"] = sql
        captured["rows"] = list(rows)
        captured["page_size"] = page_size

    monkeypatch.setattr(gp_insert_module, "execute_values", fake_execute_values)

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="gp",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["id", "value"],
        rows=[(1, pd.NA), (2, float("nan"))],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 2
    assert captured["sql"] == 'INSERT INTO schema.stage_table ("id", "value") VALUES %s'
    assert captured["rows"] == [(1, None), (2, None)]
    assert captured["page_size"] == 2
    assert connection.commit_calls == 1


def test_insert_rows_batch_trino_normalizes_values_and_splits_chunks() -> None:
    connection = FakeDbapiConnection()
    progress_updates: list[int] = []

    inserted_rows = load_sql_table_module.insert_rows_batch(
        connection_type="trino",
        connection_ref={"connection": connection},
        table_name="schema.stage_table",
        columns=["id", "label"],
        rows=[(1.9, "a"), (2, pd.NA), (3, "c")],
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
        target_column_types={"id": "bigint", "label": "varchar"},
        trino_insert_chunk_size=2,
        on_progress=progress_updates.append,
    )

    assert inserted_rows == 3
    assert connection.executed == [
        'INSERT INTO schema.stage_table ("id", "label") VALUES '
        "(CAST(? AS bigint), CAST(? AS varchar)), (CAST(? AS bigint), CAST(? AS varchar))",
        'INSERT INTO schema.stage_table ("id", "label") VALUES '
        "(CAST(? AS bigint), CAST(? AS varchar))",
    ]
    assert connection.executed_params == [
        [1, "a", 2, None],
        [3, "c"],
    ]
    assert progress_updates == [2, 1]
    assert trino_insert_module.normalize_value({"x": "я", "n": None}, "varchar") == (
        '{"x":"я","n":null}'
    )
    assert trino_insert_module.normalize_value([3, {"ok": True}], "varchar") == ('[3,{"ok":true}]')


def test_insert_table_batch_normalizes_decimal_for_clickhouse() -> None:
    client = FakeClickHouseClient()
    connection_ref = {"connection": client}
    batch = pd.DataFrame(
        {
            "amount": [Decimal("1.20"), None],
            "label": ["ok", None],
            "count": [1, 2],
        }
    )

    inserted_rows = load_sql_table_module.insert_table_batch(
        connection_type="ch",
        connection_ref=connection_ref,
        table_name="schema.stage_table",
        batch=batch,
        retry_fn=lambda **kwargs: kwargs["operation"](1),
        retry_cnt=1,
        timeout_increment=0,
    )

    assert inserted_rows == 2
    assert len(client.calls) == 1

    inserted_df = client.calls[0]["df"]
    assert isinstance(inserted_df, pd.DataFrame)
    assert inserted_df["amount"].tolist() == [1.2, None]
    assert inserted_df["label"].tolist() == ["ok", None]
    assert inserted_df["count"].tolist() == [1, 2]

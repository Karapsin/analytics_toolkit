from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    SimpleNamespace,
    keys_module,
    make_ch_config,
    make_gp_config,
    make_trino_config,
    pytest,
    transfer_api_module,
    transfer_options_module,
)


def test_normalize_transfer_keys_accepts_list_keys_in_order() -> None:
    keys = keys_module.normalize_transfer_keys(["event_date", " store_id "])

    assert keys == [
        keys_module.TransferKey(name="event_date", expression="event_date"),
        keys_module.TransferKey(name="store_id", expression="store_id"),
    ]


def test_normalize_transfer_keys_accepts_mapping_expression_key() -> None:
    keys = keys_module.normalize_transfer_keys({" user_id_suffix ": " right(user_id, 1) "})

    assert keys == [
        keys_module.TransferKey(
            name="user_id_suffix",
            expression="right(user_id, 1)",
        ),
    ]


def test_normalize_transfer_keys_accepts_simple_string_key() -> None:
    keys = keys_module.normalize_transfer_keys(" event_date ")

    assert keys == [
        keys_module.TransferKey(name="event_date", expression="event_date"),
    ]


@pytest.mark.parametrize(
    ("transfer_keys", "match"),
    [
        ({"right(user_id, 1)": "right(user_id, 1)"}, "Invalid entry"),
        ({"bucket": " "}, "must not be empty"),
        ({"bucket": 1}, "mapping values must be strings"),
        ({" bucket ": "id", "bucket": "id"}, "placeholder names must be unique"),
    ],
)
def test_normalize_transfer_keys_rejects_invalid_mapping_entries(
    transfer_keys: Any,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        keys_module.normalize_transfer_keys(transfer_keys)


@pytest.mark.parametrize(
    ("transfer_keys", "match"),
    [
        ("right(user_id, 1)", "For SQL expressions, use mapping form"),
        (["event_date", "event_date "], "placeholder names must be unique"),
        ("1event_date", "Invalid entry"),
        ("event date", "Invalid entry"),
    ],
)
def test_normalize_transfer_keys_rejects_invalid_simple_names(
    transfer_keys: Any,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        keys_module.normalize_transfer_keys(transfer_keys)


def test_normalize_transfer_slices_accepts_single_key_mapping_values() -> None:
    keys, expressions, values, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id from events where {user_id_suffix}",
        transfer_keys={"user_id_suffix": "right(user_id, 1)"},
        transfer_key_values=["0", "1"],
        concurrency=1,
    )

    assert keys == ["user_id_suffix"]
    assert expressions == {"user_id_suffix": "right(user_id, 1)"}
    assert values == {"user_id_suffix": ["0", "1"]}
    assert [transfer_slice.values for transfer_slice in slices] == [("0",), ("1",)]
    assert "(right(user_id, 1)) = '0'" in slices[0].source_sql


def test_normalize_transfer_slices_accepts_single_key_sequence_values() -> None:
    keys, expressions, values, slices, concurrency = keys_module.normalize_transfer_slices(
        source_sql="select id, event_date from events where {event_date};",
        transfer_keys="event_date",
        transfer_key_values=["2025-01-01", "2025-01-02"],
        concurrency=2,
    )

    assert keys == ["event_date"]
    assert expressions == {"event_date": "event_date"}
    assert values == {"event_date": ["2025-01-01", "2025-01-02"]}
    assert concurrency == 2
    assert [transfer_slice.values for transfer_slice in slices] == [
        ("2025-01-01",),
        ("2025-01-02",),
    ]
    assert "analytics_toolkit_transfer_source" not in slices[0].source_sql
    assert "(event_date) = '2025-01-01'" in slices[0].source_sql


def test_normalize_transfer_slices_builds_multi_key_cartesian_values() -> None:
    keys, expressions, values, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql=("select id, event_date from events where {event_date} and {user_id_suffix}"),
        transfer_keys={
            "event_date": "event_date",
            "user_id_suffix": "right(user_id, 1)",
        },
        transfer_key_values={
            "event_date": ["2025-01-01", "2025-01-02"],
            "user_id_suffix": ["0", "1"],
        },
        concurrency=3,
    )

    assert keys == ["event_date", "user_id_suffix"]
    assert expressions == {
        "event_date": "event_date",
        "user_id_suffix": "right(user_id, 1)",
    }
    assert values == {
        "event_date": ["2025-01-01", "2025-01-02"],
        "user_id_suffix": ["0", "1"],
    }
    assert [transfer_slice.values for transfer_slice in slices] == [
        ("2025-01-01", "0"),
        ("2025-01-01", "1"),
        ("2025-01-02", "0"),
        ("2025-01-02", "1"),
    ]
    assert "(event_date) = '2025-01-01'\n  AND (right(user_id, 1)) = '0'" in (
        slices[0].predicate_sql
    )
    assert "where (event_date) = '2025-01-01' and (right(user_id, 1)) = '0'" in slices[0].source_sql


def test_normalize_transfer_slices_leaves_unknown_brace_text() -> None:
    _keys, _expressions, _values, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql="select '{not_a_transfer_key}' as token where {id}",
        transfer_keys="id",
        transfer_key_values=[1],
        concurrency=1,
    )

    assert "{not_a_transfer_key}" in slices[0].source_sql


@pytest.mark.parametrize(
    ("transfer_keys", "transfer_key_values", "concurrency", "match"),
    [
        ("event_date", ["2025-01-01", "2025-01-01"], 1, "duplicate"),
        ("event_date", [], 1, "must not be empty"),
        (["event_date", "bucket"], {"event_date": ["2025-01-01"]}, 1, "missing"),
        ("event_date", {"event_date": ["2025-01-01"], "bucket": ["0"]}, 1, "extra"),
        (None, ["2025-01-01"], 1, "requires transfer_keys"),
        ("event_date", None, 1, "requires explicit"),
        (None, None, 2, "concurrency > 1"),
        ("event_date", ["2025-01-01"], 0, "positive integer"),
        ("event_date", ["2025-01-01"], True, "positive integer"),
    ],
)
def test_normalize_transfer_slices_rejects_invalid_inputs(
    transfer_keys: Any,
    transfer_key_values: Any,
    concurrency: Any,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        keys_module.normalize_transfer_slices(
            source_sql="select id from events",
            transfer_keys=transfer_keys,
            transfer_key_values=transfer_key_values,
            concurrency=concurrency,
        )


def test_normalize_transfer_slices_rejects_missing_placeholder() -> None:
    with pytest.raises(ValueError, match=r"Missing placeholder: \{event_date\}"):
        keys_module.normalize_transfer_slices(
            source_sql="select id from events",
            transfer_keys="event_date",
            transfer_key_values=["2025-01-01"],
            concurrency=1,
        )


def test_normalize_transfer_slices_rejects_multi_statement_rendered_slice() -> None:
    with pytest.raises(ValueError, match="rendered slice SQL"):
        keys_module.normalize_transfer_slices(
            source_sql="select id from events where {bad_expr}",
            transfer_keys={"bad_expr": "id) = 1; select 2 where (id"},
            transfer_key_values=[1],
            concurrency=1,
        )


def test_normalize_transfer_slices_rejects_multi_statement_source_sql() -> None:
    with pytest.raises(ValueError, match="exactly one SQL statement"):
        keys_module.normalize_transfer_slices(
            source_sql="select 1; select 2",
            transfer_keys="id",
            transfer_key_values=[1],
            concurrency=1,
        )


@pytest.mark.parametrize(
    ("value", "expected_predicate"),
    [
        ("2025-01-01", "(event_date) = '2025-01-01'"),
        (None, "(event_date) IS NULL"),
    ],
)
def test_normalize_transfer_slices_replaces_every_placeholder_occurrence(
    value: Any,
    expected_predicate: str,
) -> None:
    _keys, _expressions, _values, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql=(
            "select id from current_events where {event_date} "
            "union all select id from archived_events where {event_date}"
        ),
        transfer_keys="event_date",
        transfer_key_values=[value],
        concurrency=1,
    )

    assert slices[0].source_sql.count(expected_predicate) == 2
    assert "{event_date}" not in slices[0].source_sql


def test_normalize_transfer_slices_replaces_keys_with_different_occurrence_counts() -> None:
    _keys, _expressions, _values, slices, _concurrency = keys_module.normalize_transfer_slices(
        source_sql=(
            "select id from events where {event_date} and {bucket} "
            "union all select id from archived_events where {event_date}"
        ),
        transfer_keys=["event_date", "bucket"],
        transfer_key_values={"event_date": ["2025-01-01"], "bucket": [7]},
        concurrency=1,
    )

    assert slices[0].source_sql.count("(event_date) = '2025-01-01'") == 2
    assert slices[0].source_sql.count("(bucket) = 7") == 1
    assert "{event_date}" not in slices[0].source_sql
    assert "{bucket}" not in slices[0].source_sql


def test_resolve_adaptive_batch_bounds_defaults_clamps_and_validates() -> None:
    assert transfer_options_module.resolve_adaptive_batch_bounds(
        batch_size=500,
        min_batch_size=1_000,
        max_batch_size=None,
        target_batch_seconds=None,
        min_batch_seconds=12,
        max_batch_seconds=15,
        adaptive_batch_size=True,
    ) == (500, 2_000, 12.0, 12.0, 15.0)
    assert transfer_options_module.resolve_adaptive_batch_bounds(
        batch_size=500,
        min_batch_size=100,
        max_batch_size=None,
        target_batch_seconds=20,
        min_batch_seconds=None,
        max_batch_seconds=15,
        adaptive_batch_size=True,
        unlimited_default_max=True,
    ) == (100, None, 15.0, None, 15.0)
    with pytest.raises(ValueError, match="min_batch_seconds"):
        transfer_options_module.resolve_adaptive_batch_bounds(
            batch_size=10,
            min_batch_size=1,
            max_batch_size=20,
            target_batch_seconds=10,
            min_batch_seconds=20,
            max_batch_seconds=10,
            adaptive_batch_size=True,
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"adaptive_batch_size": 1}, "adaptive_batch_size"),
        ({"batch_size": 0}, "batch_size"),
        ({"min_batch_size": 0}, "min_batch_size"),
        ({"max_batch_size": 0}, "max_batch_size"),
        ({"target_batch_seconds": "bad"}, "target_batch_seconds"),
        ({"target_batch_seconds": 0}, "target_batch_seconds"),
        ({"batch_size": 20, "max_batch_size": 10}, "max_batch_size"),
        ({"min_batch_size": 20, "max_batch_size": 30}, "min_batch_size"),
    ],
)
def test_resolve_adaptive_batch_bounds_rejects_invalid_combinations(
    override: dict[str, Any],
    message: str,
) -> None:
    values: dict[str, Any] = {
        "batch_size": 10,
        "min_batch_size": 1,
        "max_batch_size": 20,
        "target_batch_seconds": 10,
        "min_batch_seconds": None,
        "max_batch_seconds": None,
        "adaptive_batch_size": True,
    }
    values.update(override)
    with pytest.raises(ValueError, match=message):
        transfer_options_module.resolve_adaptive_batch_bounds(**values)


@pytest.mark.parametrize("value", [True, "0.1", 0, 1, float("inf")])
def test_resolve_adaptive_batch_size_step_rejects_invalid(value: Any) -> None:
    with pytest.raises(ValueError, match="adaptive_batch_size_step"):
        transfer_options_module.resolve_adaptive_batch_size_step(value)


@pytest.mark.parametrize("value", [True, "1", 0, float("nan")])
def test_resolve_positive_number_rejects_invalid(value: Any) -> None:
    with pytest.raises(ValueError, match="limit"):
        transfer_options_module.resolve_positive_number(value, "limit")


def test_resolve_target_adaptation_mode_branches() -> None:
    with pytest.raises(ValueError, match="target_rows_per_second"):
        transfer_options_module.resolve_target_adaptation_mode(
            adaptive_batch_size=True,
            target_rows_per_second=1,
            target_batch_seconds=None,
            target_batch_memory_mb=None,
        )
    assert (
        transfer_options_module.resolve_target_adaptation_mode(
            adaptive_batch_size=False,
            target_rows_per_second=True,
            target_batch_seconds=1,
            target_batch_memory_mb=1,
        )
        is True
    )
    with pytest.raises(ValueError, match="Only one"):
        transfer_options_module.resolve_target_adaptation_mode(
            adaptive_batch_size=True,
            target_rows_per_second=False,
            target_batch_seconds=1,
            target_batch_memory_mb=None,
        )
    assert (
        transfer_options_module.resolve_target_adaptation_mode(
            adaptive_batch_size=True,
            target_rows_per_second=True,
            target_batch_seconds=None,
            target_batch_memory_mb=1,
        )
        is False
    )
    assert (
        transfer_options_module.resolve_target_adaptation_mode(
            adaptive_batch_size=True,
            target_rows_per_second=True,
            target_batch_seconds=1,
            target_batch_memory_mb=None,
        )
        is False
    )


@pytest.mark.parametrize("value", [True, "1", 0, -1, float("inf")])
def test_resolve_target_batch_memory_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(ValueError, match="target_batch_memory_mb"):
        transfer_options_module.resolve_target_batch_memory(value)


@pytest.mark.parametrize("value", [True, "0", -0.1, float("nan")])
def test_resolve_target_rows_per_second_deadband_rejects_invalid(value: Any) -> None:
    with pytest.raises(ValueError, match="target_rows_per_second_deadband"):
        transfer_options_module.resolve_target_rows_per_second_deadband(value)


@pytest.mark.parametrize("value", [True, 1.5, 0, -1])
def test_resolve_target_rows_per_second_window_rejects_invalid(value: Any) -> None:
    with pytest.raises(ValueError, match="target_rows_per_second_window"):
        transfer_options_module.resolve_target_rows_per_second_window(value)


def test_resolve_trino_mode_delegates_to_target_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, str | None, str | None]] = []
    monkeypatch.setattr(
        transfer_options_module,
        "get_backend_adapter",
        lambda _backend: SimpleNamespace(
            resolve_transfer_staging_mode=lambda mode, **kwargs: (
                calls.append(
                    (
                        mode,
                        kwargs["s3_transfer_staging_schema"],
                        kwargs["s3_transfer_staging_location"],
                    )
                )
                or "parquet"
            )
        ),
    )
    assert (
        transfer_options_module.resolve_trino_mode(
            "auto",
            target_backend="trino",
            s3_transfer_staging_schema="hive.scratch",
            s3_transfer_staging_location="s3://bucket",
        )
        == "parquet"
    )
    assert calls == [("auto", "hive.scratch", "s3://bucket")]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"to_table": "   "}, "to_table"),
        ({"target_rows_per_second": 1}, "target_rows_per_second"),
        ({"validate_row_count": 1}, "validate_row_count"),
        ({"ch_count_limit_read": 1}, "ch_count_limit_read"),
        ({"ch_only_shard": 1}, "ch_only_shard"),
    ],
)
def test_transfer_option_matrix_rejects_invalid_values(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    values: dict[str, Any] = {
        "from_db": "gp",
        "to_db": "trino",
        "from_sql": "select id from source_table",
        "to_table": "sandbox.target",
        "dry_run": True,
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        transfer_api_module.transfer_table(**values)


def test_transfer_option_memory_resolvers_return_bytes_and_validate_bounds() -> None:
    assert transfer_options_module.resolve_target_batch_memory(None) == (None, None)
    assert transfer_options_module.resolve_target_batch_memory(0.5) == (0.5, 524_288)
    assert transfer_options_module.resolve_target_batch_memory_limits(
        min_batch_memory_mb=0.25,
        max_batch_memory_mb=0.5,
    ) == (0.25, 262_144, 0.5, 524_288)
    with pytest.raises(ValueError, match="min_batch_memory_mb"):
        transfer_options_module.resolve_target_batch_memory_limits(
            min_batch_memory_mb=2,
            max_batch_memory_mb=1,
        )


def test_transfer_options_accepts_from_table_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db="source",
        to_db="target",
        from_table="sandbox.source_table",
        to_table="sandbox.target",
    )

    assert options.source_table == "sandbox.source_table"
    assert options.source_sql == "SELECT * FROM sandbox.source_table"


def test_transfer_options_default_and_none_write_modes_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "source": make_gp_config("source"),
        "target": make_gp_config("target"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )
    kwargs = {
        "from_db": "source",
        "to_db": "target",
        "from_sql": "select id from source_table",
        "to_table": "sandbox.target",
    }

    default_options = transfer_api_module.build_transfer_options(**kwargs)
    none_options = transfer_api_module.build_transfer_options(**kwargs, write_mode=None)

    assert default_options.write_mode == "append"
    assert default_options.replace_target_table is False
    assert none_options.write_mode == "append"
    assert none_options.replace_target_table is False


def test_transfer_options_defaults_use_time_target_mode_when_not_explicit() -> None:
    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        target_batch_seconds=None,
    )

    assert options.target_rows_per_second is True
    assert options.target_batch_seconds == 10.0


@pytest.mark.parametrize(
    ("source_key", "source_config"),
    [
        ("gp", make_gp_config("gp")),
        ("ch", make_ch_config("ch")),
        ("trino_a", make_trino_config("trino_a")),
    ],
)
def test_transfer_options_enable_parquet_staging_for_trino_target_with_location(
    monkeypatch: pytest.MonkeyPatch,
    source_key: str,
    source_config: Any,
) -> None:
    target_config = make_trino_config("trino_b")
    configs = {source_key: source_config, "trino_b": target_config}
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db=source_key,
        to_db="trino_b",
        from_sql="select id from source_table",
        to_table="sandbox.target",
    )

    assert options.trino_mode == "parquet"
    assert options.transfer_staging_schema == "object_storage.sandbox"
    assert options.s3_transfer_staging_location == "s3://bucket/tmp/analytics_toolkit_transfer"

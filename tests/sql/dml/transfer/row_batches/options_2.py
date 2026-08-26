from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    make_gp_config,
    make_trino_config,
    models_module,
    pytest,
    transfer_api_module,
)


def test_transfer_options_explicit_values_mode_disables_parquet_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        trino_mode="values",
    )

    assert options.trino_mode == "values"
    assert options.transfer_staging_schema == "object_storage.sandbox"
    assert options.s3_transfer_staging_location == "s3://bucket/tmp/analytics_toolkit_transfer"


def test_transfer_options_keep_row_batch_staging_when_trino_location_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config(
            "trino",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        ),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
    )

    assert options.trino_mode == "values"
    assert options.transfer_staging_schema == "object_storage.sandbox"
    assert options.s3_transfer_staging_location is None


def test_transfer_options_progress_defaults_to_false() -> None:
    options = models_module.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id from source_table",
        target_table="sandbox.target",
        batch_size=2,
    )

    assert options.progress is False


def test_transfer_options_reject_explicit_mode_for_non_trino_target(
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

    with pytest.raises(ValueError, match="to_db has type 'trino'"):
        transfer_api_module.build_transfer_options(
            from_db="source",
            to_db="target",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            trino_mode="values",
        )


def test_transfer_options_reject_explicit_parquet_without_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config(
            "trino",
            s3_transfer_staging_schema="hive.sandbox",
            s3_transfer_staging_location=None,
        ),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    with pytest.raises(ValueError, match="s3_transfer_staging_location"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            trino_mode="parquet",
        )


def test_transfer_options_reject_invalid_trino_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp": make_gp_config("gp"),
        "trino": make_trino_config("trino"),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    with pytest.raises(ValueError, match="trino_mode"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            trino_mode="execute_values",
        )


def test_transfer_options_reject_same_key_before_parquet_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_trino_config("trino")
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: config,
    )

    with pytest.raises(ValueError, match="from_db and to_db must be different"):
        transfer_api_module.build_transfer_options(
            from_db="trino",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
        )


def test_transfer_options_rejects_both_source_inputs_before_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_get_connection_config(_db_key: str) -> Any:
        raise AssertionError("connection config should not be loaded")

    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        fail_get_connection_config,
    )

    with pytest.raises(ValueError, match="Provide only one of from_sql or from_table"):
        transfer_api_module.build_transfer_options(
            from_db="source",
            to_db="target",
            from_sql="select id from source_table",
            from_table="source_table",
            to_table="sandbox.target",
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"from_sql": " "}, "from_sql must not be empty"),
        ({"from_table": " "}, "from_table must not be empty"),
    ],
)
def test_transfer_options_rejects_empty_source_input_before_connections(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, str],
    match: str,
) -> None:
    def fail_get_connection_config(_db_key: str) -> Any:
        raise AssertionError("connection config should not be loaded")

    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        fail_get_connection_config,
    )

    with pytest.raises(ValueError, match=match):
        transfer_api_module.build_transfer_options(
            from_db="source",
            to_db="target",
            to_table="sandbox.target",
            **kwargs,
        )


def test_transfer_options_rejects_gp_insert_chunk_size_for_non_gp_target() -> None:
    with pytest.raises(
        ValueError,
        match="gp_insert_chunk_size can only be used when to_db has type 'gp'",
    ):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            gp_insert_chunk_size=10_000,
        )


@pytest.mark.parametrize("gp_insert_chunk_size", [0, -1])
def test_transfer_options_rejects_invalid_gp_insert_chunk_size(
    gp_insert_chunk_size: int,
) -> None:
    with pytest.raises(
        ValueError,
        match="gp_insert_chunk_size must be a positive integer",
    ):
        transfer_api_module.build_transfer_options(
            from_db="trino",
            to_db="gp",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            gp_insert_chunk_size=gp_insert_chunk_size,
        )


def test_transfer_options_rejects_inverted_batch_memory_bounds() -> None:
    with pytest.raises(ValueError, match="min_batch_memory_mb"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_memory_mb=64,
            max_batch_memory_mb=32,
            target_batch_memory_mb=64,
        )


def test_transfer_options_rejects_inverted_batch_seconds_bounds() -> None:
    with pytest.raises(ValueError, match="min_batch_seconds"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_seconds=20.0,
            max_batch_seconds=10.0,
        )


def test_transfer_options_rejects_missing_source_input_before_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_get_connection_config(_db_key: str) -> Any:
        raise AssertionError("connection config should not be loaded")

    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        fail_get_connection_config,
    )

    with pytest.raises(ValueError, match="Provide exactly one of from_sql or from_table"):
        transfer_api_module.build_transfer_options(
            from_db="source",
            to_db="target",
            to_table="sandbox.target",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "target_rows_per_second": False,
            "target_batch_seconds": 10.0,
        },
        {
            "target_rows_per_second": False,
            "target_batch_memory_mb": 16,
        },
        {
            "target_batch_seconds": 10.0,
            "target_batch_memory_mb": 16,
        },
    ],
)
def test_transfer_options_rejects_multiple_adaptation_targets(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Only one transfer batch target"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            **kwargs,
        )


def test_transfer_options_resolve_adaptive_bounds_and_validate() -> None:
    options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
    )

    assert options.min_batch_size == 100
    assert options.max_batch_size == 400
    assert options.adaptive_batch_size_step == 0.1
    assert options.target_rows_per_second_window == 5
    assert options.target_rows_per_second_deadband == 0.15

    custom_window_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        adaptive_batch_size_step=0.25,
        target_rows_per_second_window=3,
        target_rows_per_second_deadband=0.05,
    )
    assert custom_window_options.adaptive_batch_size_step == 0.25
    assert custom_window_options.target_rows_per_second_window == 3
    assert custom_window_options.target_rows_per_second_deadband == 0.05

    memory_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
        target_batch_memory_mb=64,
    )

    assert memory_options.target_batch_memory_mb == 64.0
    assert memory_options.target_batch_memory_bytes == 64 * 1024 * 1024
    assert memory_options.max_batch_size is None

    capped_memory_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
        target_batch_memory_mb=64,
        max_batch_size=1_000,
    )

    assert capped_memory_options.max_batch_size == 1_000

    with pytest.raises(ValueError, match="min_batch_size"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            batch_size=100,
            min_batch_size=101,
        )

    with pytest.raises(ValueError, match="max_batch_size"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            batch_size=100,
            max_batch_size=99,
        )

    bounded_time_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        batch_size=100,
        target_batch_seconds=10,
        min_batch_seconds=15.0,
        max_batch_seconds=30.0,
    )
    assert bounded_time_options.target_batch_seconds == 15.0
    assert bounded_time_options.min_batch_seconds == 15.0
    assert bounded_time_options.max_batch_seconds == 30.0

    bounded_memory_options = transfer_api_module.build_transfer_options(
        from_db="gp",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
        target_batch_memory_mb=64,
        min_batch_memory_mb=32,
        max_batch_memory_mb=512,
    )
    assert bounded_memory_options.target_batch_memory_mb == 64.0
    assert bounded_memory_options.min_batch_memory_mb == 32.0
    assert bounded_memory_options.max_batch_memory_mb == 512.0


def test_transfer_options_use_source_connection_staging_schema_for_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = {
        "gp_source": make_gp_config(
            "gp_source",
            transfer_staging_schema="source_scratch",
        ),
        "trino": make_trino_config(
            "trino",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        ),
    }
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda db_key: configs[db_key],
    )

    options = transfer_api_module.build_transfer_options(
        from_db="gp_source",
        to_db="trino",
        from_sql="select id from source_table",
        to_table="sandbox.target",
    )

    assert options.source_transfer_staging_schema == "source_scratch"
    assert options.source_transfer_staging_username == "source_user"


@pytest.mark.parametrize(
    "adaptive_batch_size_step",
    [0, -0.1, 1, 1.1, float("nan"), float("inf"), True, "0.1"],
)
def test_transfer_options_validate_adaptive_batch_size_step(
    adaptive_batch_size_step: Any,
) -> None:
    with pytest.raises(ValueError, match="adaptive_batch_size_step"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            adaptive_batch_size_step=adaptive_batch_size_step,
        )


@pytest.mark.parametrize(
    "min_batch_memory_mb,max_batch_memory_mb",
    [
        (0, None),
        (None, 0),
        (True, None),
        ("16", None),
    ],
)
def test_transfer_options_validate_batch_memory_bounds(
    min_batch_memory_mb: Any,
    max_batch_memory_mb: Any,
) -> None:
    match = "min_batch_memory_mb" if min_batch_memory_mb is not None else "max_batch_memory_mb"
    with pytest.raises(ValueError, match=match):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_memory_mb=min_batch_memory_mb,
            max_batch_memory_mb=max_batch_memory_mb,
        )


@pytest.mark.parametrize(
    "min_batch_seconds,max_batch_seconds",
    [
        (0, None),
        (None, 0),
        (True, None),
        ("10", None),
    ],
)
def test_transfer_options_validate_batch_seconds_bounds(
    min_batch_seconds: Any,
    max_batch_seconds: Any,
) -> None:
    match = "min_batch_seconds" if min_batch_seconds is not None else "max_batch_seconds"
    with pytest.raises(ValueError, match=match):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            min_batch_seconds=min_batch_seconds,
            max_batch_seconds=max_batch_seconds,
        )


@pytest.mark.parametrize(
    "target_rows_per_second_deadband",
    [-0.1, float("nan"), float("inf"), True, "0.1"],
)
def test_transfer_options_validate_rows_per_second_deadband(
    target_rows_per_second_deadband: Any,
) -> None:
    with pytest.raises(ValueError, match="target_rows_per_second_deadband"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            target_rows_per_second_deadband=target_rows_per_second_deadband,
        )


@pytest.mark.parametrize(
    "target_rows_per_second_window",
    [0, -1, 1.2, True, "5", None],
)
def test_transfer_options_validate_rows_per_second_window(
    target_rows_per_second_window: Any,
) -> None:
    with pytest.raises(ValueError, match="target_rows_per_second_window"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            target_rows_per_second_window=target_rows_per_second_window,
        )

from __future__ import annotations

from tests.sql._support.partitions import (
    Any,
    InvalidSqlInputError,
    SimpleNamespace,
    UnsupportedConnectionTypeError,
    _stub_leaf_partition_discovery,
    gp_maintenance_module,
    importlib,
    pd,
    pytest,
    table_ops_module,
)


def test_gp_analyze_partitioned_table_handles_empty_and_invalid_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
    monkeypatch.setattr(
        read_sql_module,
        "read_sql",
        lambda *_args, **_kwargs: pd.DataFrame(columns=["schema_name", "relation_name"]),
    )
    assert (
        gp_maintenance_module.gp_analyze_partitioned_table("gp", "analytics.empty_parent") is None
    )

    monkeypatch.setattr(
        read_sql_module,
        "read_sql",
        lambda *_args, **_kwargs: pd.DataFrame(columns=["unexpected"]),
    )
    with pytest.raises(RuntimeError, match="invalid result"):
        gp_maintenance_module.gp_analyze_partitioned_table("gp", "analytics.orders", dry_run=True)


def test_gp_analyze_partitioned_table_rejects_invalid_parent_and_unmatched_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(InvalidSqlInputError, match="fully qualified"):
        gp_maintenance_module.gp_analyze_partitioned_table("gp", object(), dry_run=True)
    with pytest.raises(InvalidSqlInputError, match="valid fully qualified"):
        gp_maintenance_module.gp_analyze_partitioned_table("gp", "analytics..broken", dry_run=True)

    _stub_leaf_partition_discovery(monkeypatch, ["analytics.orders_1_prt_a"])
    with pytest.raises(InvalidSqlInputError, match="identify leaf partitions"):
        gp_maintenance_module.gp_analyze_partitioned_table(
            "gp",
            "analytics.orders",
            "analytics.orders_1_prt_missing",
            dry_run=True,
        )

    monkeypatch.setattr(
        gp_maintenance_module,
        "get_connection_config",
        lambda _key: SimpleNamespace(backend="trino", connection_key="trino"),
    )
    with pytest.raises(UnsupportedConnectionTypeError, match="requires a gp"):
        gp_maintenance_module.gp_analyze_partitioned_table(
            "trino", "analytics.orders", dry_run=True
        )


@pytest.mark.parametrize("concurrency", [0, True, 1.5])
def test_gp_analyze_partitioned_table_validates_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="integer >= 1"):
        gp_maintenance_module.gp_analyze_partitioned_table(
            "gp",
            "analytics.orders",
            "analytics.orders_1_prt_a",
            concurrency=concurrency,
            dry_run=True,
        )


@pytest.mark.parametrize(
    ("partition_names", "message"),
    [
        ([], "must not be empty"),
        (["orders"], "schema-qualified"),
        (["analytics.orders", "analytics.orders"], "duplicates"),
        ([1], "must contain strings"),
    ],
)
def test_gp_analyze_partitioned_table_validates_partition_names(
    partition_names: Any,
    message: str,
) -> None:
    with pytest.raises(InvalidSqlInputError, match=message):
        gp_maintenance_module.gp_analyze_partitioned_table(
            "gp", "analytics.orders", partition_names, dry_run=True
        )


def test_gp_create_partitions_rejects_non_gp_alias() -> None:
    with pytest.raises(UnsupportedConnectionTypeError, match="requires a gp"):
        table_ops_module.gp_create_partitions(
            "trino",
            "sandbox.events",
            days=["2026-05-01"],
            dry_run=True,
        )


def test_gp_create_partitions_validates_exactly_one_input() -> None:
    with pytest.raises(InvalidSqlInputError, match="Exactly one"):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            only_generate_sql=True,
        )

    with pytest.raises(InvalidSqlInputError, match="Exactly one"):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            days=["2026-05-01"],
            months=["2026-05-01"],
            only_generate_sql=True,
        )

    with pytest.raises(InvalidSqlInputError, match="non-empty sequence"):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            days=[],
            only_generate_sql=True,
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"days": ["not-a-date"]}, "valid ISO date"),
        ({"values": [" "]}, "empty strings"),
        (
            {"intervals": [{"start": "2026-05-02", "end": "2026-05-01"}]},
            "after interval start",
        ),
        ({"intervals": [{"start": "2026-05-01"}]}, "ISO date"),
        ({"days": ["2026-05-01"], "name_template": "p"}, "name_template"),
        ({"days": ["2026-05-01"], "name_template": "p_{}_{}"}, "name_template"),
        ({"days": ["2026-05-01"], "name_template": "{}"}, "unquoted SQL identifier"),
        (
            {
                "intervals": [
                    {
                        "name": "bad-name",
                        "start": "2026-05-01",
                        "end": "2026-05-02",
                    }
                ]
            },
            "unquoted SQL identifier",
        ),
        ({"days": ["2026-05-01"], "table": " "}, "Table name"),
    ],
)
def test_gp_create_partitions_validates_invalid_inputs(
    kwargs: dict[str, Any],
    match: str,
) -> None:
    table = kwargs.pop("table", "sandbox.events")

    with pytest.raises(InvalidSqlInputError, match=match):
        table_ops_module.gp_create_partitions(
            "gp",
            table,
            only_generate_sql=True,
            **kwargs,
        )


@pytest.mark.parametrize(
    ("argument", "values", "match"),
    [
        ("weeks", ["2026-05-05"], "Monday"),
        ("months", ["2026-05-02"], "month starts"),
        ("years", ["2026-02-01"], "year starts"),
    ],
)
def test_gp_create_partitions_validates_period_starts(
    argument: str,
    values: list[str],
    match: str,
) -> None:
    with pytest.raises(InvalidSqlInputError, match=match):
        table_ops_module.gp_create_partitions(
            "gp",
            "sandbox.events",
            only_generate_sql=True,
            **{argument: values},
        )

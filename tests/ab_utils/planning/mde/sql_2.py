from __future__ import annotations

from tests.ab_utils._support.metrics import (
    SimpleNamespace,
    compute_mde_from_sql,
    compute_mde_sql_native,
    pd,
    planning_module,
    pytest,
    warnings,
)


def test_compute_mde_from_sql_parallel_load_raises_sql_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    parallel_kwargs: dict[str, object] = {}

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del kwargs
        assert db_key == "analytics"
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [2],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-01")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        parallel_kwargs.update(kwargs)
        return {
            str(task["name"]): pd.DataFrame(
                {"user_id": [1, 2], "orders": [1.0, 2.0]},
            )
            for task in tasks
        }

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )
    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            outliers_quantile=1,
            concurrency=11,
        )

    assert parallel_kwargs["concurrency"] == 11
    assert parallel_kwargs["hard_concurrency_cap"] == 11


def test_compute_mde_from_sql_rejects_missing_table_or_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_table = SimpleNamespace(
        exists=False,
        columns={},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: missing_table,
    )
    with pytest.raises(ValueError, match="does not exist"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )

    missing_column = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: missing_column,
    )
    with pytest.raises(ValueError, match="Missing metric column"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_from_sql_rejects_nulls_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )

    def fake_null_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del db_key, kwargs
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [4],
                    "null_user_rows": [1],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-02")],
                }
            )
        raise AssertionError("duplicate query should not run after null validation fails")

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.read",
        fake_null_read,
    )
    with pytest.raises(ValueError, match="must not contain missing values"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )

    def fake_duplicate_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del db_key, kwargs
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [4],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-02")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [1]})
        raise AssertionError("aggregate query should not run after duplicate validation fails")

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.read",
        fake_duplicate_read,
    )
    with pytest.raises(ValueError, match="unique user-day rows"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_sql_native_rejects_missing_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planning_module.sql_facade,
        "table_info",
        lambda *_args: SimpleNamespace(exists=False),
    )
    with pytest.raises(ValueError, match="does not exist"):
        compute_mde_sql_native(
            "db",
            "public.missing",
            metric_columns=["metric"],
            group_sizes=[2],
            exp_days=[1],
            start_dt=None,
        )


def test_compute_mde_sql_native_warns_once_for_each_metric_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns=["user", "dt", "metric"],
        resolved_table="public.events",
        table="events",
        backend="gp",
    )
    monkeypatch.setattr(planning_module.sql_facade, "table_info", lambda *_args: table_info)
    monkeypatch.setattr(
        planning_module,
        "_resolve_mde_options",
        lambda **_kwargs: {
            "days": [1, 1],
            "pre_exp_days": None,
            "control_share": 0.5,
            "planned_splits": [{"group_size": 10, "control_n": 5, "test_n": 5}],
            "start_dt": None,
        },
    )
    monkeypatch.setattr(
        planning_module,
        "_validate_sql_mde_source_rows",
        lambda **_kwargs: {
            "min_date": pd.Timestamp("2026-01-01"),
            "max_date": pd.Timestamp("2026-01-01"),
        },
    )
    monkeypatch.setattr(
        planning_module,
        "_load_sql_native_mde_stats",
        lambda **_kwargs: {
            (0, 1): {
                "avg": 2.0,
                "var": 1.0,
                "cuped_pair_n": 0,
                "cuped_pre_var": None,
                "cuped_adjusted_var": None,
            }
        },
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE") as caught:
        result = compute_mde_sql_native(
            "db",
            "public.events",
            user_id="user",
            metric_columns=["metric"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
        )

    assert len(caught) == 1
    assert result.shape[0] == 2
    assert result["mde_abs_cuped"].isna().all()


@pytest.mark.parametrize(
    ("loaded", "error"),
    [({"mde_native_0_1": []}, TypeError), ({"mde_native_0_1": pd.DataFrame()}, ValueError)],
)
def test_sql_native_mde_loader_rejects_invalid_results(
    monkeypatch: pytest.MonkeyPatch,
    loaded: dict[str, object],
    error: type[Exception],
) -> None:
    monkeypatch.setattr(
        planning_module.sql_facade, "parallel_sql", lambda *_args, **_kwargs: loaded
    )
    kwargs = {
        "concurrency": 1,
        "db_key": "db",
        "backend": "gp",
        "source": '"public"."events"',
        "sql_where": None,
        "user_id": "user",
        "date_column": "dt",
        "metric_definitions": [{"kind": "mean", "metric_key": "metric", "column": "metric"}],
        "aggregation_policies": {"metric": "sum"},
        "days_values": [1],
        "windows": {
            1: {
                "outcome_start": pd.Timestamp("2026-01-01"),
                "pre_start": None,
                "pre_days": 1,
            }
        },
        "outliers_quantile": 0.99,
        "outliers_policy": "truncate",
        "print_queries": False,
        "retry_cnt": 0,
        "timeout_increment": 0,
        "query_label": None,
    }
    with pytest.raises(error):
        planning_module._load_sql_native_mde_stats(**kwargs)


def test_sql_native_mde_loader_returns_first_row(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = {"mde_native_0_1": pd.DataFrame({"avg": [2.0], "var": [3.0]})}
    monkeypatch.setattr(
        planning_module.sql_facade, "parallel_sql", lambda *_args, **_kwargs: loaded
    )
    result = planning_module._load_sql_native_mde_stats(
        concurrency=1,
        db_key="db",
        backend="gp",
        source='"public"."events"',
        sql_where=None,
        user_id="user",
        date_column="dt",
        metric_definitions=[{"kind": "mean", "metric_key": "metric", "column": "metric"}],
        aggregation_policies={"metric": "sum"},
        days_values=[1],
        windows={
            1: {
                "outcome_start": pd.Timestamp("2026-01-01"),
                "pre_start": None,
                "pre_days": 1,
            }
        },
        outliers_quantile=0.99,
        outliers_policy="truncate",
        print_queries=False,
        retry_cnt=0,
        timeout_increment=0,
        query_label=None,
    )
    assert result == {(0, 1): {"avg": 2.0, "var": 3.0}}


@pytest.mark.parametrize(
    ("source_rows", "message"),
    [
        (pd.DataFrame(), "returned no rows"),
        (
            pd.DataFrame(
                {
                    "row_count": [0],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [None],
                    "max_dt": [None],
                }
            ),
            "at least one user-day",
        ),
        (
            pd.DataFrame(
                {
                    "row_count": [1],
                    "null_user_rows": [1],
                    "null_date_rows": [0],
                    "min_dt": ["2026-01-01"],
                    "max_dt": ["2026-01-01"],
                }
            ),
            "user.*missing values",
        ),
        (
            pd.DataFrame(
                {
                    "row_count": [1],
                    "null_user_rows": [0],
                    "null_date_rows": [1],
                    "min_dt": ["2026-01-01"],
                    "max_dt": ["2026-01-01"],
                }
            ),
            "dt.*missing values",
        ),
    ],
)
def test_sql_mde_source_validation_rejects_empty_null_and_zero_sources(
    monkeypatch: pytest.MonkeyPatch, source_rows: pd.DataFrame, message: str
) -> None:
    monkeypatch.setattr(planning_module, "_read_sql_mde_query", lambda **_kwargs: source_rows)
    with pytest.raises(ValueError, match=message):
        planning_module._validate_sql_mde_source_rows(
            db_key="db",
            backend="gp",
            source='"public"."events"',
            sql_where=None,
            user_id="user",
            date_column="dt",
            print_queries=False,
            retry_cnt=0,
            timeout_increment=0,
            query_label=None,
        )


def test_sql_mde_window_task_deduplicates_matching_window() -> None:
    tasks: list[dict[str, object]] = []
    task_names = {(pd.Timestamp("2026-01-01"), 1): "existing"}
    result = planning_module._add_sql_mde_window_load_task(
        tasks=tasks,
        task_names_by_window=task_names,
        task_name="new",
        db_key="db",
        backend="gp",
        source='"public"."events"',
        sql_where=None,
        user_id="user",
        date_column="dt",
        columns=["metric"],
        aggregation_policies={"metric": "sum"},
        start_date=pd.Timestamp("2026-01-01"),
        days=1,
        print_queries=False,
        retry_cnt=0,
        timeout_increment=0,
        query_label=None,
    )
    assert result == "existing"
    assert tasks == []


def test_read_sql_mde_user_window_delegates_and_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        planning_module,
        "_build_sql_mde_user_window_query",
        lambda **kwargs: captured.setdefault("builder", kwargs) and "SELECT metric",
    )

    def fake_read(**kwargs: object) -> pd.DataFrame:
        captured["reader"] = kwargs
        return pd.DataFrame({"user": [1], "metric": [2.0], "extra": [3.0]})

    monkeypatch.setattr(planning_module, "_read_sql_mde_query", fake_read)
    result = planning_module._read_sql_mde_user_window(
        db_key="db",
        backend="gp",
        source='"public"."events"',
        sql_where="active",
        user_id="user",
        date_column="dt",
        columns=["metric"],
        aggregation_policies={"metric": "sum"},
        start_date=pd.Timestamp("2026-01-01"),
        days=1,
        print_queries=True,
        retry_cnt=2,
        timeout_increment=3,
        query_label="mde",
    )
    assert result.to_dict("list") == {"user": [1], "metric": [2.0]}
    assert captured["reader"] == {
        "db_key": "db",
        "query": "SELECT metric",
        "print_queries": True,
        "retry_cnt": 2,
        "timeout_increment": 3,
        "query_label": "mde",
    }

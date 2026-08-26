from __future__ import annotations

from tests.ab_utils._support.metrics import (
    Any,
    SimpleNamespace,
    _manual_cuped_adjusted_variance,
    compute_mde,
    compute_mde_from_sql,
    compute_mde_sql_native,
    inspect,
    pd,
    planning_module,
    pytest,
    threading,
    time,
    warnings,
)


def test_compute_mde_from_sql_concurrency_defaults_to_one() -> None:
    assert inspect.signature(compute_mde_from_sql).parameters["concurrency"].default == 1
    assert inspect.signature(compute_mde_sql_native).parameters["concurrency"].default == 1


def test_mde_sql_where_and_required_column_validation() -> None:
    with pytest.raises(TypeError, match="string or None"):
        planning_module._normalize_sql_where(1)
    with pytest.raises(ValueError, match="must not be empty"):
        planning_module._normalize_sql_where("  ")
    with pytest.raises(ValueError, match="Column 'user'"):
        planning_module._validate_sql_source_required_columns(
            column_names=["dt"], user_id="user", date_column="dt"
        )
    with pytest.raises(ValueError, match="Column 'dt'"):
        planning_module._validate_sql_source_required_columns(
            column_names=["user"], user_id="user", date_column="dt"
        )


def test_mde_sql_result_and_date_coercion_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError, match="did not return a dataframe"):
        planning_module._normalize_sql_mde_window_frame(
            result=[], user_id="user", columns=["metric"]
        )
    monkeypatch.setattr(planning_module.sql_facade, "read", lambda *args, **kwargs: [])
    with pytest.raises(TypeError, match="did not return a dataframe"):
        planning_module._read_sql_mde_query(
            db_key="db",
            query="SELECT 1",
            print_queries=False,
            retry_cnt=0,
            timeout_increment=0,
            query_label=None,
        )
    with pytest.raises(ValueError, match="minimum date is missing"):
        planning_module._coerce_sql_date(None, "minimum date")
    with pytest.raises(ValueError, match="must be datelike"):
        planning_module._coerce_sql_date(object(), "minimum date")


def test_compute_mde_from_sql_matches_dataframe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "orders": [1.0, 2.0, 3.0, 4.0, 3.0, 4.0, 6.0, 8.0, 5.0, 6.0, 10.0, 13.0],
        }
    )
    expected = compute_mde(
        source_df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
    )
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    queries: list[str] = []

    def fake_table_info(db_key: str, table: str) -> SimpleNamespace:
        assert db_key == "analytics"
        assert table == "sandbox.events"
        return table_info

    def fake_read(
        db_key: str,
        query: str,
        **kwargs: object,
    ) -> pd.DataFrame:
        assert db_key == "analytics"
        assert kwargs["query_label"] == "mde"
        queries.append(query)
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [len(source_df)],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-04")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        assert kwargs["concurrency"] == 1
        frames: dict[str, pd.DataFrame] = {}
        for task in tasks:
            assert task["db_key"] == "analytics"
            assert task["query_label"] == "mde"
            query = task["query"]
            assert isinstance(query, str)
            queries.append(query)
            if "CAST(\"dt\" AS DATE) >= DATE '2024-01-03'" in query:
                frames[str(task["name"])] = pd.DataFrame(
                    {"user_id": [1, 2, 3], "orders": [7.0, 14.0, 23.0]}
                )
                continue
            if "CAST(\"dt\" AS DATE) >= DATE '2024-01-01'" in query:
                frames[str(task["name"])] = pd.DataFrame(
                    {"user_id": [1, 2, 3], "orders": [3.0, 7.0, 11.0]}
                )
                continue
            raise AssertionError(f"Unexpected aggregate query:\n{query}")
        return frames

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        fake_table_info,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.read",
        fake_read,
    )
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    result = compute_mde_from_sql(
        "analytics",
        "sandbox.events",
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
        query_label="mde",
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any('FROM "sandbox"."events"' in query for query in queries)


def test_compute_mde_sql_native_uses_compact_sql_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3],
            "dt": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"] * 3),
            "orders": [1.0, 2.0, 3.0, 4.0, 3.0, 4.0, 6.0, 8.0, 5.0, 6.0, 10.0, 13.0],
        }
    )
    expected = compute_mde(
        source_df,
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
    )
    expected_row = expected.iloc[0]
    pre_values = pd.Series([3.0, 7.0, 11.0])
    outcome_values = pd.Series([7.0, 14.0, 23.0])
    cuped_pre_var = float(pre_values.var(ddof=1))
    cuped_adjusted_var = _manual_cuped_adjusted_variance(outcome_values, pre_values)
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    queries: list[str] = []

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        assert db_key == "analytics"
        assert kwargs["query_label"] == "mde-native"
        queries.append(query)
        if "COUNT(*) AS row_count" in query:
            return pd.DataFrame(
                {
                    "row_count": [len(source_df)],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-04")],
                }
            )
        if "duplicate_user_day_rows" in query:
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        assert kwargs["concurrency"] == 1
        frames: dict[str, pd.DataFrame] = {}
        for task in tasks:
            assert task["db_key"] == "analytics"
            assert task["query_label"] == "mde-native"
            query = task["query"]
            assert isinstance(query, str)
            queries.append(query)
            assert "GROUP BY" in query
            assert "ORDER BY" not in query
            assert "MAX(value)" in query
            assert "VAR_SAMP" in query
            assert "COVAR_SAMP" in query
            frames[str(task["name"])] = pd.DataFrame(
                {
                    "avg": [expected_row["avg"]],
                    "var": [expected_row["var"]],
                    "cuped_pair_n": [3],
                    "cuped_pre_var": [cuped_pre_var],
                    "cuped_adjusted_var": [cuped_adjusted_var],
                }
            )
        return frames

    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    result = compute_mde_sql_native(
        "analytics",
        "sandbox.events",
        user_id="user_id",
        metric_columns=["orders"],
        group_sizes=[10],
        exp_days=[2],
        start_dt="2024-01-03",
        outliers_quantile=1,
        query_label="mde-native",
    )

    pd.testing.assert_frame_equal(result, expected)
    assert any('FROM "sandbox"."events"' in query for query in queries)


def test_compute_mde_sql_native_generates_backend_specific_stats_sql() -> None:
    metric_definition = {"kind": "mean", "metric_key": "orders", "column": "orders"}

    gp_query = planning_module._build_sql_native_mde_stats_query(
        backend="gp",
        source='"sandbox"."events"',
        sql_where="country = 'US'",
        user_id="user_id",
        date_column="dt",
        metric_definition=metric_definition,
        aggregation_policies={"orders": "sum"},
        outcome_start=pd.Timestamp("2024-01-03"),
        outcome_days=2,
        pre_start=pd.Timestamp("2024-01-01"),
        pre_days=2,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    )
    trino_query = planning_module._build_sql_native_mde_stats_query(
        backend="trino",
        source='"sandbox"."events"',
        sql_where=None,
        user_id="user_id",
        date_column="dt",
        metric_definition=metric_definition,
        aggregation_policies={"orders": "sum"},
        outcome_start=pd.Timestamp("2024-01-03"),
        outcome_days=2,
        pre_start=pd.Timestamp("2024-01-01"),
        pre_days=2,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    )
    ch_query = planning_module._build_sql_native_mde_stats_query(
        backend="ch",
        source="`sandbox`.`events`",
        sql_where=None,
        user_id="user_id",
        date_column="dt",
        metric_definition=metric_definition,
        aggregation_policies={"orders": "sum"},
        outcome_start=pd.Timestamp("2024-01-03"),
        outcome_days=2,
        pre_start=pd.Timestamp("2024-01-01"),
        pre_days=2,
        outliers_quantile=0.95,
        outliers_policy="truncate",
    )

    assert "PERCENTILE_CONT" in gp_query
    assert "VAR_SAMP" in gp_query
    assert "COVAR_SAMP" in gp_query
    assert "(country = 'US')" in gp_query
    assert "approx_percentile" in trino_query
    assert "quantileExact" in ch_query
    assert "varSamp" in ch_query
    assert "covarSamp" in ch_query


def test_compute_mde_from_sql_applies_where_to_validation_and_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    queries: list[str] = []

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del db_key, kwargs
        queries.append(query)
        assert "(country = 'US')" in query
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
            return pd.DataFrame({"duplicate_user_day_rows": [0]})
        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        del kwargs
        frames: dict[str, pd.DataFrame] = {}
        for task in tasks:
            query = task["query"]
            assert isinstance(query, str)
            queries.append(query)
            assert "(country = 'US')" in query
            frames[str(task["name"])] = pd.DataFrame({"user_id": [1, 2], "orders": [10.0, 12.0]})
        return frames

    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )

    with pytest.warns(UserWarning, match="Could not compute CUPED MDE"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            sql_where="country = 'US'",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            outliers_quantile=1,
        )

    assert len(queries) == 3


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_compute_mde_from_sql_rejects_invalid_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10],
            exp_days=[1],
            start_dt=None,
            concurrency=concurrency,
        )


def test_compute_mde_from_sql_parallelizes_day_size_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_df = pd.DataFrame(
        {
            "user_id": [1, 1, 2, 2, 3, 3],
            "dt": pd.to_datetime(
                [
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-01",
                    "2024-01-02",
                    "2024-01-01",
                    "2024-01-02",
                ]
            ),
            "orders": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        expected = compute_mde(
            source_df,
            user_id="user_id",
            metric_columns=["orders"],
            group_sizes=[10, 20],
            exp_days=[1, 2],
            start_dt=None,
            outliers_quantile=1,
        )

    table_info = SimpleNamespace(
        exists=True,
        columns={"user_id": "int", "dt": "date", "orders": "double precision"},
        backend="gp",
        table="sandbox.events",
        resolved_table=None,
    )
    events: list[str] = []
    active_loads = 0
    max_active_loads = 0
    active_compute_tasks = 0
    max_active_compute_tasks = 0
    lock = threading.Lock()
    load_barrier = threading.Barrier(2)
    parallel_kwargs: dict[str, object] = {}
    compute_calls: list[tuple[int, int]] = []
    real_compute_task = planning_module._compute_sql_mde_day_size_rows

    def fake_read(db_key: str, query: str, **kwargs: object) -> pd.DataFrame:
        del kwargs
        assert db_key == "analytics"
        if "COUNT(*) AS row_count" in query:
            with lock:
                events.append("validation:stats")
            return pd.DataFrame(
                {
                    "row_count": [len(source_df)],
                    "null_user_rows": [0],
                    "null_date_rows": [0],
                    "min_dt": [pd.Timestamp("2024-01-01")],
                    "max_dt": [pd.Timestamp("2024-01-02")],
                }
            )
        if "duplicate_user_day_rows" in query:
            with lock:
                events.append("validation:duplicates")
            return pd.DataFrame({"duplicate_user_day_rows": [0]})

        raise AssertionError(f"Unexpected direct aggregate query:\n{query}")

    def fake_parallel_sql(tasks: object, **kwargs: object) -> dict[str, pd.DataFrame]:
        nonlocal active_loads, max_active_loads
        task_list = list(tasks)
        with lock:
            assert events == ["validation:stats", "validation:duplicates"]
            events.append("parallel_sql")
        parallel_kwargs.update(kwargs)
        assert len(task_list) == 2

        def run_task(task: dict[str, object]) -> tuple[str, pd.DataFrame]:
            nonlocal active_loads, max_active_loads
            assert task["type"] == "read"
            assert task["db_key"] == "analytics"
            query = task["query"]
            assert isinstance(query, str)
            with lock:
                events.append("aggregate")
                active_loads += 1
                max_active_loads = max(max_active_loads, active_loads)
            try:
                load_barrier.wait(timeout=1)
                if "CAST(\"dt\" AS DATE) < DATE '2024-01-02'" in query:
                    frame = pd.DataFrame({"user_id": [1, 2, 3], "orders": [1.0, 3.0, 5.0]})
                elif "CAST(\"dt\" AS DATE) < DATE '2024-01-03'" in query:
                    frame = pd.DataFrame({"user_id": [1, 2, 3], "orders": [3.0, 7.0, 11.0]})
                else:
                    raise AssertionError(f"Unexpected aggregate query:\n{query}")
                return str(task["name"]), frame
            finally:
                with lock:
                    active_loads -= 1

        with planning_module.ThreadPoolExecutor(
            max_workers=int(kwargs["concurrency"]),
        ) as executor:
            return dict(executor.map(run_task, task_list))

    def recording_compute_task(*args: object, **kwargs: object) -> object:
        nonlocal active_compute_tasks, max_active_compute_tasks
        with lock:
            assert "parallel_sql" in events
            active_compute_tasks += 1
            max_active_compute_tasks = max(
                max_active_compute_tasks,
                active_compute_tasks,
            )
            compute_calls.append((int(kwargs["days"]), int(kwargs["split"]["group_size"])))
        try:
            time.sleep(0.02)
            return real_compute_task(*args, **kwargs)
        finally:
            with lock:
                active_compute_tasks -= 1

    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.table_info",
        lambda db_key, table: table_info,
    )
    monkeypatch.setattr("analytics_toolkit.ab_utils.planning.sql_facade.read", fake_read)
    monkeypatch.setattr(
        "analytics_toolkit.ab_utils.planning.sql_facade.parallel_sql",
        fake_parallel_sql,
    )
    monkeypatch.setattr(
        planning_module,
        "_compute_sql_mde_day_size_rows",
        recording_compute_task,
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        result = compute_mde_from_sql(
            "analytics",
            "sandbox.events",
            metric_columns=["orders"],
            group_sizes=[10, 20],
            exp_days=[1, 2],
            start_dt=None,
            outliers_quantile=1,
            concurrency=2,
        )

    pd.testing.assert_frame_equal(result, expected)
    assert parallel_kwargs == {
        "concurrency": 2,
        "fail_fast": True,
        "progress": False,
        "hard_concurrency_cap": 5,
    }
    assert max_active_loads == 2
    assert events.count("aggregate") == 2
    assert max_active_compute_tasks == 2
    assert sorted(compute_calls) == [(1, 10), (1, 20), (2, 10), (2, 20)]
    cuped_warnings = [
        warning for warning in caught if "Could not compute CUPED MDE" in str(warning.message)
    ]
    assert len(cuped_warnings) == 2

from __future__ import annotations

from tests.sql._support.lifecycle import (
    LifecycleAdapter,
    maintenance,
    pytest,
)


def test_analyze_table_builds_plan_or_calls_adapter(
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    connection = object()

    plan = maintenance.analyze_table(
        "gp",
        connection,
        "schema.table",
        query_label="q",
        return_sql=True,
    )
    result = maintenance.analyze_table("gp", connection, "schema.table")

    assert plan.sqls == ["ANALYZE schema.table"]
    assert plan.statements[0].phase == "analyze"
    assert plan.metadata.statement_count == 1
    assert result is None
    assert lifecycle_adapter.calls[-1][0] == "analyze_table"


def test_analyze_table_returns_skipped_plan_for_noop_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = LifecycleAdapter(analyze=False)
    monkeypatch.setattr(maintenance, "resolve_connection_backend", lambda _value: "ch")
    monkeypatch.setattr(maintenance, "get_backend_adapter", lambda _backend: adapter)

    assert maintenance.analyze_table("alias", object(), "db.table") is None
    plan = maintenance.analyze_table(
        "alias",
        object(),
        "db.table",
        query_label="q",
        dry_run=True,
    )

    assert plan.options == {"skipped": True, "reason": "ch analyze is a no-op"}
    assert plan.metadata.statement_count == 0
    assert plan.sqls == []


def test_drop_table_returns_plan_or_executes_and_waits(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle_adapter: LifecycleAdapter,
) -> None:
    monkeypatch.setattr(
        maintenance,
        "build_drop_table_sql",
        lambda *_args, **_kwargs: "DROP TABLE schema.target",
    )
    connection = object()

    plan = maintenance.drop_table(
        "gp",
        connection,
        "schema.target",
        query_label="q",
        dry_run=True,
    )
    result = maintenance.drop_table(
        "gp",
        connection,
        "schema.target",
        ch_cluster="cluster",
        if_exists=False,
        wait_for_absence=True,
    )

    assert plan.sqls == ["DROP TABLE schema.target"]
    assert plan.statements[0].phase == "drop_target"
    assert result is None
    assert [call[0] for call in lifecycle_adapter.calls] == [
        "drop_table",
        "wait_for_table_absence",
    ]

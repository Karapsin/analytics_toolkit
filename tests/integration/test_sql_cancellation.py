from __future__ import annotations

# ruff: noqa: I001, TC002

import os
import uuid

import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param
from tests.integration.support.identity import query_label
from tests.integration.support.query_workers import (
    QueryWorker,
    find_labelled_query,
    long_running_query,
    poll_until,
)
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
BACKENDS = ("gp", "trino", "ch")


def _enabled(backend: str) -> bool:
    return backend != "gp" or os.environ.get("SQL_INTEGRATION_GP") == "1"


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _gone(db_key: str, label: str) -> bool:
    active = sql.show_queries(db_key, state="active", retry_cnt=1)
    if active.empty:
        return True
    return not active["query"].astype(str).str.contains(label, regex=False).any()


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"query.cancel.{backend}", backend) for backend in BACKENDS],
)
def test_cancel_visible_labelled_query(
    backend: str,
    resource_registry: ResourceRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    label = _label("single_cancel")
    worker = resource_registry.worker(
        QueryWorker(backend, long_running_query(backend), label).start()
    )
    row = find_labelled_query(backend, label)
    assert row["query_id"] not in {None, ""}
    assert row["state"] == "active"
    assert label in str(row["query"])
    worker.query_id = resource_registry.query(backend, row["query_id"])

    result = sql.cancel_queries(
        backend,
        [row["query_id"]],
        concurrency=1,
        print_queries=True,
        retry_cnt=1,
        query_label=_label("cancellation_operation"),
    )
    assert result["query_id"].tolist() == [row["query_id"]]
    assert result.iloc[0]["status"]
    assert capsys.readouterr().out
    worker.join()
    poll_until(
        lambda: _gone(backend, label),
        description=f"cancelled query to leave active state: {label}",
    )


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"query.cancel_all.{backend}", backend) for backend in BACKENDS],
)
def test_cancel_all_terminates_two_workers(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    labels = [_label("cancel_all_first"), _label("cancel_all_second")]
    workers = [
        resource_registry.worker(QueryWorker(backend, long_running_query(backend), label).start())
        for label in labels
    ]
    rows = [find_labelled_query(backend, label) for label in labels]
    for worker, row in zip(workers, rows):
        worker.query_id = resource_registry.query(backend, row["query_id"])

    result = sql.cancel_queries(
        backend,
        cancel_all=True,
        concurrency=2,
        retry_cnt=1,
        query_label=_label("cancel_all_operation"),
    )
    cancelled_ids = set(result["query_id"].tolist())
    assert {row["query_id"] for row in rows} <= cancelled_ids
    for worker in workers:
        worker.join()
    for label in labels:
        poll_until(
            lambda label=label: _gone(backend, label),
            description=f"cancel-all query to leave active state: {label}",
        )


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"query.edge.{backend}.missing", backend) for backend in BACKENDS],
)
def test_cancel_missing_query_id_has_deterministic_backend_result(backend: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    missing_id: int | str = 2_000_000_000 if backend == "gp" else "missing-query-id"
    if backend == "trino":
        with pytest.raises(Exception, match=r"(?i)query|not found|unknown"):
            sql.cancel_queries(backend, [missing_id], concurrency=2, retry_cnt=1)
        return
    result = sql.cancel_queries(backend, [missing_id], concurrency=2, retry_cnt=1)
    assert result["query_id"].tolist() == [missing_id]
    if backend == "gp":
        assert result["cancelled"].tolist() == [False]
    else:
        assert result["status"].tolist() == ["submitted"]


def _short_running_query(backend: str) -> str:
    if backend == "gp":
        return "SELECT pg_sleep(2)"
    if backend == "ch":
        return (
            "SELECT count() FROM numbers(20) WHERE sleepEachRow(0.1) = 0 SETTINGS max_block_size=1"
        )
    return "SELECT count(*) FROM UNNEST(sequence(1, 25000000))"


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"query.edge.{backend}.finished", backend) for backend in BACKENDS],
)
def test_cancel_finished_query_id_does_not_reactivate_query(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    label = _label("finished_cancel")
    worker = resource_registry.worker(
        QueryWorker(backend, _short_running_query(backend), label).start()
    )
    row = find_labelled_query(backend, label)
    query_id = row["query_id"]
    worker.join(timeout=30)
    poll_until(lambda: _gone(backend, label), description=f"finished query {label}")
    if backend == "trino":
        with pytest.raises(Exception, match=r"(?i)query|not found|finished"):
            sql.cancel_queries(backend, [query_id], retry_cnt=1)
    else:
        result = sql.cancel_queries(backend, [query_id], retry_cnt=1)
        assert result["query_id"].tolist() == [query_id]
    assert _gone(backend, label)

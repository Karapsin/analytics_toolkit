"""Explicit zero-submission outcomes cannot hide unrelated or ambiguous CREATEs."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.creation_outcomes import record_skipped_optional_facade
from analytics_toolkit.sql.backends.ch.routing import _RoutingClickHouse
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.creation_resources import (
    CreatedTableResource,
    CreationScope,
    SkippedCreation,
    record_skipped_creation,
    scoped_creation_observer,
    skipped_creation_count,
    submitted_creation_resource,
)
from analytics_toolkit.sql.execution.backend_options import backend_operation
from sqlglot import exp, parse_one

from tests.sql._support.availability_writes import (
    configure_optional_facade,
    observe_created_resources,
)
from tests.sql._support.outage import Client


@pytest.mark.parametrize(
    "mismatch",
    ["required", "reachable", "backend", "alias", "scope", "dynamic", "cluster", "table"],
)
def test_skipped_facade_outcome_requires_its_exact_empty_optional_candidate(
    outage: Any, mismatch: str
) -> None:
    configure_optional_facade(outage, "http", partial=mismatch == "reachable")
    connection = sql.get_sql_connection("target")
    resource = CreatedTableResource(
        "db.expected", "ch", CreationScope("cluster", cluster="edge"), connection_key="target"
    )
    if mismatch == "backend":
        resource = replace(resource, backend="gp")
    elif mismatch == "alias":
        resource = replace(resource, connection_key="source")
    elif mismatch == "scope":
        resource = replace(resource, scope=CreationScope())
    elif mismatch == "dynamic":
        resource = replace(resource, scope=CreationScope("cluster"))
    elif mismatch == "cluster":
        resource = replace(resource, scope=CreationScope("cluster", cluster="core"))
    elif mismatch == "table":
        resource = replace(resource, name="db.foreign")
    records = []
    try:
        with backend_operation() as state:
            session = connection._get_session(state)
            scope = session.scope("core" if mismatch == "required" else "edge")
            with scoped_creation_observer(records.append), submitted_creation_resource(resource):
                with pytest.raises(sql.ClickHouseClusterTopologyError, match="exact optional"):
                    record_skipped_optional_facade(scope, "db", "expected")
                assert skipped_creation_count() == 0
    finally:
        connection.close()
    assert records == []


def test_skipped_creation_outcomes_cannot_escape_their_observer_or_change_candidate() -> None:
    resource = CreatedTableResource("db.expected", "ch")
    assert record_skipped_creation(SkippedCreation(resource)) is False
    records = []
    with scoped_creation_observer(records.append), submitted_creation_resource(resource):
        with pytest.raises(InvalidSqlInputError, match="match the submitted candidate"):
            record_skipped_creation(SkippedCreation(replace(resource, name="db.foreign")))
        assert skipped_creation_count() == 0
    assert records == []
    assert skipped_creation_count() == 0


@pytest.mark.parametrize("transport", ["http", "native"])
def test_ambiguous_actual_facade_create_retains_candidate_instead_of_reporting_skip(
    outage: Any, monkeypatch: Any, transport: str
) -> None:
    cluster = configure_optional_facade(outage, transport, partial=True)
    captured, _dropped = observe_created_resources(monkeypatch)
    command = Client.command

    def lose_ack(client: Any, statement: str, **kwargs: Any) -> Any:
        result = command(client, statement, **kwargs)
        parsed = parse_one(statement, read=_RoutingClickHouse)
        if isinstance(parsed, exp.Create):
            target = parsed.this.this if isinstance(parsed.this, exp.Schema) else parsed.this
            if target.name == "uncertain_facade" and parsed.find(exp.OnCluster) is None:
                message = "Facade CREATE committed but its acknowledgement was lost."
                raise ConnectionError(message)
        return result

    monkeypatch.setattr(Client, "command", lose_ack)
    with pytest.warns(sql.ClickHouseDegradedClusterWarning), pytest.raises(
        ConnectionError, match="acknowledgement was lost"
    ):
        sql.create_table(
            "target",
            "db.uncertain_facade",
            sql="SELECT id FROM db.events",
            retry_cnt=1,
            timeout_increment=0,
        )
    assert ("h1", "db.uncertain_facade") in cluster.tables
    assert not any(resource.name.split(".")[-1] == "uncertain_facade" for resource in captured)
    assert not any(query.startswith("DROP") for _host, query, _settings in cluster.commands)

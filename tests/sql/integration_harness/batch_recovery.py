from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pandas as pd
from analytics_toolkit.sql.dml.transfer.runtime import retry

from tests.sql.integration._support.faults import FaultGate
from tests.sql.integration.fault import recovery

if TYPE_CHECKING:
    from typing import Any

    import pytest


class _Connection:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.alive = True
        self.closed = False

    def rollback(self) -> None:
        self.actions.append("rollback")

    def close(self) -> None:
        self.closed = True


class _RestartSimulation:
    def __init__(self) -> None:
        self.staged_rows: list[dict[str, Any]] = []
        self.target_rows: list[dict[str, Any]] = []
        self.actions: list[str] = []
        self.connections: list[_Connection] = []
        self.running = True

    def open_connection(self, _key: str) -> _Connection:
        assert self.running
        connection = _Connection(self.actions)
        self.connections.append(connection)
        self.actions.append("connect")
        return connection

    def stop(self, service: str) -> None:
        assert service == "greenplum"
        assert self.staged_rows == [{"row_id": 1, "value": "1"}]
        self.running = False
        for connection in self.connections:
            connection.alive = False
        self.actions.append("stop")

    def restart(self, service: str) -> None:
        assert service == "greenplum"
        self.running = True
        self.actions.append("restart")

    def wait_healthy(self, service: str) -> None:
        assert service == "greenplum"
        assert self.running
        self.actions.append("healthy")

    def insert(self, _backend, connection, _table, columns, rows, **_kwargs) -> None:
        if not self.running or not connection.alive:
            self.actions.append("failed-insert")
            msg = "server closed the connection"
            raise OSError(msg)
        self.staged_rows.extend(dict(zip(columns, row)) for row in rows)

    @staticmethod
    def immediate_retry(**kwargs: Any) -> Any:
        # Preserve the scenario's retry budget while avoiding real backoff.
        kwargs["timeout_increment"] = 0
        return retry.run_with_retry(**kwargs)

    def transfer(self, _source, alias, _query, table, **kwargs) -> int:
        attempt_module = importlib.import_module(
            "analytics_toolkit.sql.dml.transfer.flow.staged_attempt"
        )
        assert kwargs["write_mode"] == "append"
        assert kwargs["batch_size"] == 1
        ref = {"connection": self.open_connection(alias)}
        try:
            for value in range(1, 7):
                attempt_module.insert_rows_batch(
                    "gp",
                    ref,
                    table,
                    ["row_id", "value"],
                    [(value, str(value))],
                    retry_fn=self.immediate_retry,
                    retry_cnt=kwargs["retry_cnt"],
                    timeout_increment=kwargs["timeout_increment"],
                    connection_key=alias,
                    rollback_fn=retry.rollback_quietly,
                    replace_connection_fn=retry.replace_connection,
                )
            self.target_rows.extend(self.staged_rows)
            return len(self.staged_rows)
        finally:
            ref["connection"].close()

    def load(self, _alias, _table, frame, **_kwargs) -> None:
        self.target_rows.extend(frame[["row_id", "value"]].to_dict("records"))

    def read(self, *_args) -> pd.DataFrame:
        return pd.DataFrame(self.target_rows).sort_values("row_id").reset_index(drop=True)


def test_greenplum_restart_scenario_retries_failed_batch_after_service_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")
    monkeypatch.setenv("SQL_INTEGRATION_FAULT_GROUP", "database")
    monkeypatch.setenv("SQL_INTEGRATION_GP", "1")
    simulation = _RestartSimulation()
    gates: list[FaultGate] = []
    workers: list[Any] = []

    def gate_factory(**kwargs: Any) -> FaultGate:
        kwargs["timeout"] = 2
        gate = FaultGate(**kwargs)
        gates.append(gate)
        return gate

    def register_worker(worker: Any) -> Any:
        workers.append(worker)
        return worker

    monkeypatch.setattr(insert_module, "_insert_rows_backend", simulation.insert)
    monkeypatch.setattr(retry, "get_sql_connection", simulation.open_connection)
    monkeypatch.setattr(recovery, "FaultGate", gate_factory)
    monkeypatch.setattr(recovery.sql, "load_df", simulation.load)
    monkeypatch.setattr(recovery.sql, "transfer", simulation.transfer)
    monkeypatch.setattr(recovery.sql, "read", simulation.read)
    registry = SimpleNamespace(table=lambda _alias, table: table, worker=register_worker)
    scenario = (
        recovery.test_target_restart_after_first_transfer_batch_is_exact_or_contextually_ambiguous
    )
    try:
        scenario("gp", simulation, registry, monkeypatch)
    finally:
        for gate in gates:
            gate.open()
            gate.resume_after_failure()
        for worker in workers:
            worker.join(timeout=2)

    assert simulation.actions == [
        "connect",
        "stop",
        "failed-insert",
        "restart",
        "healthy",
        "rollback",
        "connect",
    ]
    assert len(simulation.connections) == 2
    assert all(connection.closed for connection in simulation.connections)
    assert [row["row_id"] for row in simulation.staged_rows] == [1, 2, 3, 4, 5, 6]
    assert sorted(row["row_id"] for row in simulation.target_rows) == [1, 2, 3, 4, 5, 6, 100]

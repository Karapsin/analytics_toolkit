"""Offline sessions shared by the headless Explorer interaction tests."""

from __future__ import annotations

from copy import copy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pandas as pd
from analytics_toolkit.sql_explorer.runtime import ExplorerCancelResult, ExplorerRunResult
from analytics_toolkit.sql_explorer.settings import ExplorerSettings
from analytics_toolkit.sql_explorer.statements import build_execution_plan


class FakeSession:
    def __init__(self) -> None:
        self.settings = ExplorerSettings()
        self.settings_warning: str | None = None
        self.database = SimpleNamespace(connection_key="gp", backend="gp")
        self.executed: list[Any] = []
        self.cancel_calls = 0

    def plan(self, sql_text: str) -> Any:
        return build_execution_plan(sql_text, self.database.backend)

    def execute(self, plan: Any, *, database: Any = None) -> ExplorerRunResult:
        self.executed.append(plan)
        return ExplorerRunResult(
            route=plan.route,
            dataframe=pd.DataFrame({"value": [1, None]}),
            displayed_rows=2,
            total_rows=2,
            truncated=False,
            status="Returned 2 row(s).",
        )

    def fork(self) -> FakeSession:
        child = copy(self)
        child.database = copy(self.database)
        return child

    def switch_database(self, key: str) -> Any:
        if key == "bad":
            message = "bad database"
            raise ValueError(message)
        self.database = SimpleNamespace(connection_key=key, backend="trino")
        return self.database

    def set_run_binding(self, value: str) -> ExplorerSettings:
        binding = "ctrl+enter" if value == "reset" else value
        self.settings = replace(self.settings, run_binding=binding)
        return self.settings

    def set_confirmation(self, *, enabled: bool) -> ExplorerSettings:
        self.settings = replace(self.settings, confirm_mutations=enabled)
        return self.settings

    def cancel_active(self) -> ExplorerCancelResult:
        self.cancel_calls += 1
        return ExplorerCancelResult(1, 1, "Cancellation requested for 1 query.")


class ErrorSession(FakeSession):
    def execute(self, plan: Any, *, database: Any = None) -> ExplorerRunResult:
        message = plan.execution_sql
        raise RuntimeError(message)


class CancelErrorSession(FakeSession):
    def cancel_active(self) -> ExplorerCancelResult:
        message = "cancel failed"
        raise RuntimeError(message)


class NoRowsSession(FakeSession):
    def execute(self, plan: Any, *, database: Any = None) -> ExplorerRunResult:
        return ExplorerRunResult(
            route=plan.route,
            dataframe=None,
            displayed_rows=0,
            total_rows=None,
            truncated=False,
            status="Executed successfully.",
        )

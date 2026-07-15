from __future__ import annotations

# ruff: noqa: BLE001, EM102, I001, SIM105, TC002, TRY003, UP037

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd
from analytics_toolkit import sql


@dataclass
class QueryWorker:
    db_key: str
    query: str
    label: str
    thread: threading.Thread | None = None
    result: pd.DataFrame | None = None
    error: BaseException | None = None
    started_at: float | None = None
    finished_at: float | None = None
    query_id: int | str | None = None
    _started: threading.Event = field(default_factory=threading.Event)

    def start(self) -> "QueryWorker":
        def target() -> None:
            self.started_at = time.monotonic()
            self._started.set()
            try:
                self.result = sql.read(
                    self.db_key,
                    self.query,
                    retry_cnt=1,
                    query_label=self.label,
                )
            except BaseException as exc:  # cancellation is the expected worker exit.
                self.error = exc
            finally:
                self.finished_at = time.monotonic()

        self.thread = threading.Thread(target=target, name=f"query-worker-{self.db_key}")
        self.thread.start()
        if not self._started.wait(timeout=5):
            raise TimeoutError(f"query worker did not start: {self.label}")
        return self

    def join(self, timeout: float = 20) -> None:
        if self.thread is None:
            return
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError(f"query worker did not terminate: {self.label}")

    def cancel(self) -> None:
        if self.thread is not None and not self.thread.is_alive():
            return
        if self.query_id is None:
            return
        try:
            sql.cancel_queries(self.db_key, [self.query_id], retry_cnt=1)
        except Exception:
            pass


def poll_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 20,
    interval: float = 0.2,
    description: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value is not None and last_value is not False:
            return last_value
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description}; last value={last_value!r}")


def find_labelled_query(db_key: str, label: str, *, timeout: float = 20) -> pd.Series:
    def locate() -> pd.Series | None:
        active = sql.show_queries(db_key, state="active", retry_cnt=1)
        if active.empty or "query" not in active:
            return None
        matches = active[active["query"].astype(str).str.contains(label, regex=False)]
        return None if matches.empty else matches.iloc[0]

    return poll_until(locate, timeout=timeout, description=f"active query {label}")


def long_running_query(backend: str) -> str:
    if backend == "gp":
        return "SELECT pg_sleep(30)"
    if backend == "ch":
        return (
            "SELECT count() FROM numbers(300) WHERE sleepEachRow(0.1) = 0 SETTINGS max_block_size=1"
        )
    return (
        "SELECT sum(sin(a.x + b.x)) "
        "FROM UNNEST(sequence(1, 10000)) AS a(x) "
        "CROSS JOIN UNNEST(sequence(1, 10000)) AS b(x)"
    )

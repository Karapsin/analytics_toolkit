"""Deterministic scheduling primitives for tab-local SQL Explorer work."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import DatabaseSelection
    from .statements import ExplorerExecutionPlan


@dataclass(frozen=True)
class ExplorerQueryJob:
    """An immutable user-query snapshot owned by one workspace tab."""

    job_id: int
    tab_id: str
    plan: ExplorerExecutionPlan
    database: DatabaseSelection


class ExplorerQueryScheduler:
    """Bounded FIFO scheduler with at most one outstanding job per tab."""

    def __init__(self, concurrency: int = 1) -> None:
        self._concurrency = self._validate_concurrency(concurrency)
        self._next_job_id = 0
        self._pending: deque[ExplorerQueryJob] = deque()
        self._active: dict[int, ExplorerQueryJob] = {}

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def set_concurrency(self, value: int) -> None:
        self._concurrency = self._validate_concurrency(value)

    def enqueue(
        self,
        tab_id: str,
        plan: ExplorerExecutionPlan,
        database: DatabaseSelection,
    ) -> ExplorerQueryJob | None:
        if self.job_for_tab(tab_id) is not None:
            return None
        self._next_job_id += 1
        job = ExplorerQueryJob(self._next_job_id, tab_id, plan, database)
        self._pending.append(job)
        return job

    def take_startable(self) -> tuple[ExplorerQueryJob, ...]:
        jobs: list[ExplorerQueryJob] = []
        while self._pending and len(self._active) < self._concurrency:
            job = self._pending.popleft()
            self._active[job.job_id] = job
            jobs.append(job)
        return tuple(jobs)

    def complete(self, job_id: int) -> ExplorerQueryJob | None:
        return self._active.pop(job_id, None)

    def remove_pending_tab(self, tab_id: str) -> ExplorerQueryJob | None:
        removed: ExplorerQueryJob | None = None
        retained: deque[ExplorerQueryJob] = deque()
        for job in self._pending:
            if job.tab_id == tab_id:
                removed = job
            else:
                retained.append(job)
        self._pending = retained
        return removed

    def job_for_tab(self, tab_id: str) -> ExplorerQueryJob | None:
        for job in self._active.values():
            if job.tab_id == tab_id:
                return job
        return next((job for job in self._pending if job.tab_id == tab_id), None)

    def is_active(self, job_id: int) -> bool:
        return job_id in self._active

    def position(self, tab_id: str) -> int | None:
        for position, job in enumerate(self._pending, start=1):
            if job.tab_id == tab_id:
                return position
        return None

    @staticmethod
    def _validate_concurrency(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            message = "Query concurrency must be a positive integer."
            raise ValueError(message)
        return value


__all__ = ["ExplorerQueryJob", "ExplorerQueryScheduler"]

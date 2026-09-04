"""Deterministic scheduling primitives for tab-local SQL Explorer work."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

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
    """FIFO scheduler with one active query per database and per tab."""

    def __init__(self) -> None:
        self._next_job_id = 0
        self._pending: deque[ExplorerQueryJob] = deque()
        self._active: dict[int, ExplorerQueryJob] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

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

    def take_startable(
        self,
        blocked_database_keys: Iterable[str] = (),
    ) -> tuple[ExplorerQueryJob, ...]:
        jobs: list[ExplorerQueryJob] = []
        unavailable = {key.casefold() for key in blocked_database_keys}
        unavailable.update(job.database.connection_key.casefold() for job in self._active.values())
        retained: deque[ExplorerQueryJob] = deque()
        while self._pending:
            job = self._pending.popleft()
            database_key = job.database.connection_key.casefold()
            if database_key in unavailable:
                retained.append(job)
                continue
            self._active[job.job_id] = job
            unavailable.add(database_key)
            jobs.append(job)
        self._pending = retained
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

    def is_database_active(self, connection_key: str) -> bool:
        normalized = connection_key.casefold()
        return any(
            job.database.connection_key.casefold() == normalized for job in self._active.values()
        )

    def position(self, tab_id: str) -> int | None:
        target = next((job for job in self._pending if job.tab_id == tab_id), None)
        if target is None:
            return None
        normalized = target.database.connection_key.casefold()
        matching_tabs = [
            job.tab_id
            for job in self._pending
            if job.database.connection_key.casefold() == normalized
        ]
        return matching_tabs.index(tab_id) + 1


__all__ = ["ExplorerQueryJob", "ExplorerQueryScheduler"]

"""Scope data replay to an already certified complete upsert payload."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from analytics_toolkit.sql.dml.table.upsert_checkpoint import UpsertCheckpoint


@dataclass(frozen=True)
class CertifiedUpsertReplay:
    connection: object = field(repr=False)
    backend: str
    target_table: str
    final_stage_table: str
    replay_sqls: tuple[str, ...] = field(repr=False)
    backend_cache: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


_CURRENT: ContextVar[CertifiedUpsertReplay | None] = ContextVar(
    "analytics_toolkit_certified_upsert_replay", default=None
)


def current_upsert_replay() -> CertifiedUpsertReplay | None:
    return _CURRENT.get()


@contextmanager
def certified_upsert_replay(
    connection: object, checkpoint: UpsertCheckpoint | None
) -> Iterator[None]:
    """Enter only after complete payload population and validation have succeeded."""
    if checkpoint is None:
        yield
        return
    if (
        not checkpoint.prepared
        or not checkpoint.backend
        or not checkpoint.target_table
        or not checkpoint.final_stage_table
        or not checkpoint.replay_sqls
    ):
        message = "Upsert data replay requires a certified complete prepared payload."
        raise ValueError(message)
    context = CertifiedUpsertReplay(
        connection,
        checkpoint.backend,
        checkpoint.target_table,
        checkpoint.final_stage_table,
        tuple(checkpoint.replay_sqls),
    )
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)

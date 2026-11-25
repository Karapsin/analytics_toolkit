"""Run-owned partition rebuild payload retained across data-write retries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UpsertCheckpoint:
    """Only certified payloads may be replayed after target mutation begins."""

    final_stage_table: str | None = None
    target_table: str | None = None
    backend: str | None = None
    replay_sqls: list[str] = field(default_factory=list)
    prepared: bool = False
    mutation_started: bool = False
    backend_state: dict[str, Any] = field(default_factory=dict, repr=False)

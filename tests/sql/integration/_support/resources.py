from __future__ import annotations

# ruff: noqa: BLE001, C901, I001, PERF203, PLR0912, TC003

import json
import subprocess
import time
from dataclasses import dataclass, field
from numbers import Integral
from pathlib import Path
from typing import Any

from analytics_toolkit import sql

RESOURCE_CLEANUP_ATTEMPTS = 30


@dataclass
class ResourceRegistry:
    root: Path
    project: str | None
    artifact_dir: Path
    tables: list[tuple[str, str, str | None]] = field(default_factory=list)
    queries: list[tuple[str, int | str]] = field(default_factory=list)
    workers: list[Any] = field(default_factory=list)
    finalizers: list[Any] = field(default_factory=list)
    minio_prefixes: list[str] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)

    def table(self, db_key: str, table: str, *, ch_cluster: str | None = None) -> str:
        self.tables.append((db_key, table, ch_cluster))
        return table

    def query(self, db_key: str, query_id: int | str) -> int | str:
        normalized_id = int(query_id) if isinstance(query_id, Integral) else query_id
        self.queries.append((db_key, normalized_id))
        return normalized_id

    def worker(self, worker: Any) -> Any:
        self.workers.append(worker)
        return worker

    def finalizer(self, finalizer: Any) -> Any:
        self.finalizers.append(finalizer)
        return finalizer

    def minio(self, prefix: str) -> str:
        self.minio_prefixes.append(prefix)
        return prefix

    def cleanup(self) -> list[str]:
        for db_key, query_id in reversed(self.queries):
            try:
                sql.cancel_queries(db_key, [query_id], retry_cnt=1)
            except Exception as exc:  # best-effort cancellation remains diagnostic.
                message = str(exc).lower()
                if not any(
                    token in message
                    for token in ("not running", "not found", "unknown query", "no such query")
                ):
                    self.cleanup_errors.append(f"cancel {db_key}:{query_id}: {exc!r}")
        for finalizer in reversed(self.finalizers):
            try:
                finalizer()
            except Exception as exc:
                self.cleanup_errors.append(f"finalizer {finalizer!r}: {exc!r}")
        for worker in reversed(self.workers):
            try:
                worker.cancel()
                worker.join(timeout=20)
            except Exception as exc:
                self.cleanup_errors.append(f"worker {worker!r}: {exc!r}")
        for db_key, table, cluster in reversed(self.tables):
            last_error: Exception | None = None
            for attempt in range(1, RESOURCE_CLEANUP_ATTEMPTS + 1):
                try:
                    sql.drop_tables(db_key, table, if_exists=True, ch_cluster=cluster)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < RESOURCE_CLEANUP_ATTEMPTS:
                        time.sleep(1)
            if last_error is not None:
                self.cleanup_errors.append(f"drop {db_key}:{table}: {last_error!r}")
        for prefix in reversed(self.minio_prefixes):
            self._remove_minio_prefix(prefix)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / "resource-registry.json").write_text(
            json.dumps(
                {
                    "tables": self.tables,
                    "queries": self.queries,
                    "finalizers": [repr(item) for item in self.finalizers],
                    "minio_prefixes": self.minio_prefixes,
                    "cleanup_errors": self.cleanup_errors,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return self.cleanup_errors

    def _remove_minio_prefix(self, prefix: str) -> None:
        if not self.project:
            return
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.root / "integration/docker-compose.yml"),
            "exec",
            "-T",
            "minio-client",
            "mc",
            "rm",
            "--recursive",
            "--force",
            f"integration/warehouse/{prefix.lstrip('/')}",
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0 and "not found" not in result.stderr.lower():
            self.cleanup_errors.append(f"minio {prefix}: {result.stderr.strip()}")

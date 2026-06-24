from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .base import BackendAdapter, BackendName, _apply_query_label
from .utils import extract_row_count


@dataclass(frozen=True)
class DbApiBackendAdapter(BackendAdapter):
    backend: BackendName
    commit_commands: bool

    def execute_command(self, connection: Any, sql: str) -> Any:
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            if self.commit_commands:
                connection.commit()
            return cursor
        except Exception:
            if self.commit_commands:
                connection.rollback()
            raise
        finally:
            cursor.close()

    def execute_commands(self, connection: Any, sqls: list[str]) -> None:
        cursor = connection.cursor()
        try:
            for sql in sqls:
                cursor.execute(sql)
            if self.commit_commands:
                connection.commit()
        except Exception:
            if self.commit_commands:
                connection.rollback()
            raise
        finally:
            cursor.close()

    def insert_from_query(
        self,
        connection: Any,
        target_table: str,
        source_sql: str,
        column_types: Mapping[str, str],
        *,
        query_label: str | None = None,
    ) -> int:
        sql = _apply_query_label(
            self.build_insert_from_query_sql(target_table, source_sql, column_types),
            query_label,
        )
        cursor = connection.cursor()
        try:
            cursor.execute(sql)
            row_count = extract_row_count(cursor)
            if self.commit_commands:
                connection.commit()
            return row_count
        except Exception:
            if self.commit_commands:
                connection.rollback()
            raise
        finally:
            cursor.close()

    def iter_source_batches(
        self,
        *,
        connection_key: str,
        connection_ref: dict[str, Any],
        query: str,
        get_batch_size: Callable[[], int],
        retry_cnt: int,
        timeout_increment: int | float,
        disable_query_limit: bool = False,
    ) -> Iterator[Any]:
        del disable_query_limit
        from ..dml.transfer.io.source import _iter_dbapi_batches

        yield from _iter_dbapi_batches(
            connection_key,
            self.backend,
            connection_ref,
            query,
            get_batch_size,
            retry_cnt=retry_cnt,
            timeout_increment=timeout_increment,
        )

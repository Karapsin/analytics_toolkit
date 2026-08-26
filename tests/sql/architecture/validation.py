from __future__ import annotations

from tests.sql._support.architecture import (
    PROJECT_ROOT,
    SQL_ROOT,
)


def test_trino_insert_chunk_size_validation_is_adapter_owned() -> None:
    checked_paths = [
        SQL_ROOT / "dml" / "load" / "load_df.py",
        SQL_ROOT / "dml" / "table" / "create_table_from_sql.py",
        SQL_ROOT / "dml" / "transfer" / "flow" / "api.py",
    ]
    forbidden_snippets = {
        "trino_insert_chunk_size is not None and",
        "trino_insert_chunk_size <= 0",
    }
    offenders: list[str] = []
    for path in checked_paths:
        text = path.read_text()
        for snippet in forbidden_snippets:
            if snippet in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {snippet}")

    assert offenders == []

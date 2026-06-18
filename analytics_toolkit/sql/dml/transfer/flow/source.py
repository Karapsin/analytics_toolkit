from __future__ import annotations


def normalize_transfer_source(
    *,
    from_sql: str | None,
    from_table: str | None,
) -> tuple[str, str | None]:
    has_from_sql = from_sql is not None
    has_from_table = from_table is not None
    if has_from_sql and has_from_table:
        raise ValueError("Provide only one of from_sql or from_table.")
    if not has_from_sql and not has_from_table:
        raise ValueError("Provide exactly one of from_sql or from_table.")

    if has_from_sql:
        source_sql = from_sql.strip() if isinstance(from_sql, str) else ""
        if not source_sql:
            raise ValueError("from_sql must not be empty.")
        return source_sql, None

    source_table = from_table.strip() if isinstance(from_table, str) else ""
    if not source_table:
        raise ValueError("from_table must not be empty.")
    return f"SELECT * FROM {source_table}", source_table

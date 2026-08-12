from __future__ import annotations

# ruff: noqa: PLR0913
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlglot import exp, parse_one

from analytics_toolkit.sql.ddl.properties import merge_ddl_properties, overlay_with_properties

from ..base import _apply_query_label

FORBIDDEN_CREDENTIAL_FIELDS = frozenset(
    {
        "access_key_id",
        "secret_access_key",
        "session_token",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    }
)

AIRFLOW_EXTRA_FIELDS = (
    "catalog",
    "schema",
    "transfer_staging_schema",
    "s3_transfer_staging_schema",
    "s3_transfer_staging_location",
    "aws_endpoint_url",
    "endpoint_url",
    "upsert_partition_drop_sql_template",
    "auth_mode",
    "http_scheme",
    "verify",
    "ca_certs",
    "insert_chunk_size",
    "request_timeout",
    "source",
    "ddl_defaults",
)


def build_parquet_stage_table_sql(
    adapter: Any,
    stage_table: str,
    column_types: Mapping[str, str] | None,
    stage_external_location: str,
    *,
    query_label: str | None = None,
    ddl_properties: Mapping[str, Any] | None = None,
) -> str:
    if column_types:
        columns_sql = ", ".join(
            f"{adapter.quote_identifier(column_name)} {_hive_parquet_stage_type(column_type)}"
            for column_name, column_type in column_types.items()
        )
    else:
        columns_sql = "<source query schema>"
    sql = (
        f"CREATE TABLE {stage_table} ({columns_sql}) "
        "WITH ("
        "format = 'PARQUET', "
        f"external_location = {trino_string_literal(stage_external_location)}"
        ")"
    )
    if ddl_properties:
        protected = {
            "format": "'PARQUET'",
            "external_location": trino_string_literal(stage_external_location),
        }
        sql = overlay_with_properties(sql, merge_ddl_properties(ddl_properties, protected))
    return _apply_query_label(sql, query_label)


def _hive_parquet_stage_type(column_type: str) -> str:
    if column_type.strip().lower() == "uuid":
        return "VARCHAR"
    if re.fullmatch(
        r"timestamp(?:\s*\(\s*\d+\s*\))?\s+with\s+time\s+zone",
        column_type.strip(),
        flags=re.IGNORECASE,
    ):
        return "VARCHAR"
    return column_type


def infer_parquet_stage_column_types_from_rows(
    adapter: Any,
    batch: Any,
) -> dict[str, str]:
    del adapter
    inferred: dict[str, str] = {}
    for index, column_name in enumerate(batch.columns):
        inferred[column_name] = _infer_trino_type_from_values(row[index] for row in batch.rows)
    return inferred


def parquet_stage_target_table_base(adapter: Any, target_table: str) -> str:
    table = parse_one(target_table, read=adapter.sqlglot_dialect, into=exp.Table)
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        raise ValueError(f"Invalid target table name: {target_table}")
    return str(table.this.this)


def trino_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _infer_trino_type_from_values(values: Any) -> str:
    for value in values:
        if value is None:
            continue
        try:
            if bool(pd.isna(value)):
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(value, bool):
            return "BOOLEAN"
        if isinstance(value, int):
            return "BIGINT"
        if isinstance(value, float):
            return "DOUBLE"
        if isinstance(value, Decimal):
            sign, digits, exponent = value.as_tuple()
            del sign
            precision = min(max(len(digits), 1), 38)
            scale = min(max(-exponent, 0), precision)
            return f"DECIMAL({precision}, {scale})"
        if isinstance(value, datetime):
            return "TIMESTAMP"
        if isinstance(value, date):
            return "DATE"
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "VARBINARY"
        return "VARCHAR"
    return "VARCHAR"

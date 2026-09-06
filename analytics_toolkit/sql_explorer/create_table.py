"""Typed create-table form values and immutable scheduled operations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from .statements import ExecutionRoute, ExplorerExecutionPlan

# Suggestions are examples, not a whitelist: backend-native types remain editable.
TYPE_SUGGESTIONS = {
    "gp": (
        "BIGINT",
        "BOOLEAN",
        "DATE",
        "DOUBLE PRECISION",
        "INTEGER",
        "JSONB",
        "NUMERIC(18,2)",
        "TEXT",
        "TIMESTAMP",
        "TIMESTAMPTZ",
        "VARCHAR(255)",
    ),
    "trino": (
        "ARRAY(VARCHAR)",
        "BIGINT",
        "BOOLEAN",
        "DATE",
        "DECIMAL(18,2)",
        "DOUBLE",
        "INTEGER",
        "MAP(VARCHAR,BIGINT)",
        "ROW(id BIGINT)",
        "TIMESTAMP",
        "VARCHAR",
    ),
    "ch": (
        "Array(String)",
        "Bool",
        "Date",
        "DateTime",
        "Decimal(18,2)",
        "Float64",
        "Int64",
        "Map(String,Int64)",
        "Nullable(String)",
        "String",
        "Tuple(id Int64)",
        "UInt64",
        "UUID",
    ),
}

# (argument, input kind); blank entries leave public function defaults intact.
COMMON_OPTIONS = (
    ("partition_by", "text"),
    ("order_by", "text"),
)
BACKEND_OPTIONS = {
    "gp": (("gp_distributed_by_key", "text"), ("gp_partitions", "json")),
    "trino": (),
    "ch": (
        *tuple(
            (name, "text")
            for name in (
                "ch_engine",
                "ch_sharding_key",
                "ch_distributed_engine_template",
                "ch_distributed_cluster",
                "ch_shard_on_cluster",
                "ch_distributed_on_cluster",
                "ch_ddl_wait_policy",
            )
        ),
        ("ch_distributed_table", "bool"),
        ("ch_ddl_ready_timeout_seconds", "float"),
        ("ch_only_shard", "bool"),
        ("ch_replace_table", "bool"),
    ),
}


@dataclass(frozen=True)
class CreateTablePlan(ExplorerExecutionPlan):
    options_json: str

    @property
    def options(self) -> dict[str, Any]:
        return dict(json.loads(self.options_json))


def creation_options(draft: dict[str, Any], backend: str) -> dict[str, Any]:
    """Validate form structure; the SQL API owns backend-specific validation."""
    name = str(draft.get("table_name", "")).strip()
    if not name:
        msg = "Enter a table name."
        raise ValueError(msg)
    skip, drop = bool(draft.get("skip_if_exists")), bool(draft.get("drop_if_exists"))
    if skip and drop:
        msg = "Choose either skip_if_exists or drop_if_exists."
        raise ValueError(msg)
    options: dict[str, Any] = {"table_name": name, "if_not_exists": skip, "drop_if_exists": drop}
    sql_mode = draft.get("source") == "from_sql"
    options["insert_data"] = bool(draft.get("insert_data")) if sql_mode else False
    if sql_mode:
        query = str(draft.get("from_sql", "")).strip()
        if not query:
            msg = "Enter source SQL."
            raise ValueError(msg)
        options["sql"] = query
    else:
        options["table_schema"] = _schema_rows(draft.get("rows", []))
    specs = COMMON_OPTIONS + BACKEND_OPTIONS.get(backend, ())
    if sql_mode:
        specs = (("source_db", "text"), *specs)
    for key, kind in specs:
        value = str(draft.get("advanced", {}).get(key, "")).strip()
        if not value:
            continue
        options[key] = _advanced_value(key, kind, value)
    return options


def _schema_rows(rows: list[tuple[str, str]]) -> dict[str, str]:
    schema: dict[str, str] = {}
    for raw_name, raw_type in rows:
        column, data_type = raw_name.strip(), raw_type.strip()
        if not column or not data_type:
            msg = "Each column needs a name and SQL type."
            raise ValueError(msg)
        if column in schema:
            msg = f"Duplicate column name: {column}"
            raise ValueError(msg)
        schema[column] = data_type
    if not schema:
        msg = "Add at least one column."
        raise ValueError(msg)
    return schema


def _advanced_value(key: str, kind: str, value: str) -> Any:
    parsed: Any
    if kind == "float":
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < 0:
            msg = f"{key} must be finite and non-negative."
            raise ValueError(msg)
    elif kind == "json":
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            msg = f"{key} must be a JSON object."
            raise ValueError(msg)
    elif kind == "bool":
        if value not in {"True", "False"}:
            msg = f"{key} must be True or False."
            raise ValueError(msg)
        parsed = value == "True"
    else:
        parsed = value
    return parsed


def creation_plan(options: dict[str, Any]) -> CreateTablePlan:
    return CreateTablePlan(
        statements=(f"CREATE TABLE {options['table_name']}",),
        execution_sql="",
        route=ExecutionRoute.EXECUTE,
        returns_rows=False,
        requires_confirmation=False,
        server_limited=False,
        options_json=json.dumps(options),
    )

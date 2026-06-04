from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


_LABEL_SAFE_RE = re.compile(r"[^A-Za-z0-9_.:=,@/+ -]+")
_AIRFLOW_CONTEXT_OBJECT_FIELDS = {
    "dag_id": ("dag", "task_instance", "ti", "task"),
    "task_id": ("task", "task_instance", "ti"),
    "run_id": ("dag_run", "task_instance", "ti"),
    "try_number": ("task_instance", "ti"),
}


def normalize_query_label(query_label: str | None) -> str | None:
    if query_label is None:
        return None
    normalized = " ".join(str(query_label).strip().split())
    if not normalized:
        return None
    normalized = _LABEL_SAFE_RE.sub("_", normalized)
    return normalized[:200]


def query_label_comment(query_label: str | None) -> str:
    normalized = normalize_query_label(query_label)
    if normalized is None:
        return ""
    return f"/* analytics_toolkit query_label={normalized} */"


def apply_query_label(sql: str, query_label: str | None) -> str:
    comment = query_label_comment(query_label)
    if not comment:
        return sql
    stripped = sql.lstrip()
    if stripped.startswith("/* analytics_toolkit query_label="):
        return sql
    return f"{comment}\n{sql}"


def airflow_query_label(
    context: Mapping[str, Any] | None = None,
    *,
    dag_id: Any | None = None,
    task_id: Any | None = None,
    run_id: Any | None = None,
    try_number: Any | None = None,
    operation: Any | None = None,
) -> str | None:
    """Build a SQL query label from Airflow task context fields."""

    if context is not None and not isinstance(context, Mapping):
        raise TypeError("context must be a mapping or None.")

    resolved_dag_id = _explicit_or_context_value(context, "dag_id", dag_id)
    resolved_task_id = _explicit_or_context_value(context, "task_id", task_id)
    resolved_run_id = _explicit_or_context_value(context, "run_id", run_id)
    resolved_try_number = _explicit_or_context_value(
        context,
        "try_number",
        try_number,
    )

    parts = [
        ("dag", resolved_dag_id),
        ("task", resolved_task_id),
        ("run", resolved_run_id),
        ("try", resolved_try_number),
        ("op", operation),
    ]
    label_parts = [
        f"{name}={text}"
        for name, value in parts
        if (text := _label_value_text(value)) is not None
    ]
    if not label_parts:
        return None
    return normalize_query_label("airflow " + " ".join(label_parts))


def _explicit_or_context_value(
    context: Mapping[str, Any] | None,
    field_name: str,
    explicit_value: Any | None,
) -> Any | None:
    if explicit_value is not None:
        return explicit_value
    if context is None:
        return None
    direct_value = context.get(field_name)
    if direct_value is not None:
        return direct_value
    for object_field in _AIRFLOW_CONTEXT_OBJECT_FIELDS[field_name]:
        context_object = context.get(object_field)
        object_value = getattr(context_object, field_name, None)
        if object_value is not None:
            return object_value
    return None


def _label_value_text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

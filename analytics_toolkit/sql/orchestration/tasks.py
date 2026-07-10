from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from analytics_toolkit.general import time_print

from ..execution.operation_runner import validate_progress_option

_SUPPORTED_TASK_TYPES = frozenset(
    {"read", "execute", "execute_read", "load_df", "transfer", "custom_sql_pipeline"}
)
_PIPELINE_TASK_TYPE = "custom_sql_pipeline"
_PROGRESS_TASK_TYPES = frozenset({"execute", "execute_read", "load_df", "transfer"})
_FORBIDDEN_TASK_ARGUMENTS = frozenset(
    {"connection", "connection_type", "connection_key", "backend"}
)
_START_COMMENT_SQL_FIELDS = {
    "read": "query",
    "execute": "query",
    "execute_read": "query",
    "transfer": "from_sql",
}


def _normalize_task_result(result: Any) -> Any:
    if result is None:
        return "success"
    return result


def _annotate_task_exception(
    exc: BaseException,
    name: str,
    task_type: str,
    kwargs: dict[str, Any],
) -> None:
    try:
        setattr(exc, "analytics_toolkit_sql_task_name", name)
        setattr(exc, "analytics_toolkit_sql_task_type", task_type)
        field, query = _task_sql_field_and_query(task_type, kwargs)
        if field is not None:
            setattr(exc, "analytics_toolkit_sql_field", field)
        if query is not None:
            setattr(exc, "analytics_toolkit_sql_query", query)
    except Exception:
        return


def _task_sql_field_and_query(
    task_type: str,
    kwargs: dict[str, Any],
) -> tuple[str | None, str | None]:
    sql_field = _START_COMMENT_SQL_FIELDS.get(task_type)
    if sql_field is None:
        return None, None

    query = kwargs.get(sql_field)
    if isinstance(query, str) and query.strip():
        return sql_field, query
    return sql_field, None


def _log_failed_sql_task(
    name: str,
    task_type: str,
    kwargs: dict[str, Any],
    exc: BaseException,
) -> None:
    sql_field, query = _task_sql_field_and_query(task_type, kwargs)
    if query is None:
        return

    time_print(
        f"SQL task {name!r} ({task_type}) failed while running {sql_field}: "
        f"{exc}\n{sql_field}:\n{query}"
    )


def _validate_tasks(
    tasks: Sequence[Mapping[str, Any]],
    *,
    start_comment: str | None,
) -> list[tuple[str, str, dict[str, Any]]]:
    default_start_comment = _validate_start_comment(
        "start_comment",
        start_comment,
    )
    if isinstance(tasks, Sequence) and not isinstance(
        tasks,
        (str, bytes, bytearray),
    ):
        return _validate_task_sequence(tasks, start_comment=default_start_comment)
    raise TypeError("tasks must be a non-empty sequence of task mappings.")


def _validate_task_sequence(
    tasks: Sequence[Mapping[str, Any]],
    *,
    start_comment: str | None,
) -> list[tuple[str, str, dict[str, Any]]]:
    if not tasks:
        raise ValueError("tasks must be a non-empty sequence.")

    task_defs: list[tuple[str, str, dict[str, Any]]] = []
    task_names: set[str] = set()
    for index, spec in enumerate(tasks):
        if not isinstance(spec, Mapping):
            raise TypeError(f"Task at index {index} must be a mapping.")
        spec_dict = dict(spec)
        task_name = spec_dict.pop("name", f"task_{index}")
        if not isinstance(task_name, str) or not task_name.strip():
            raise ValueError(
                f"Task at index {index} has invalid name; expected a non-empty string."
            )
        if task_name in task_names:
            raise ValueError(f"Duplicate SQL task name: {task_name!r}.")
        task_names.add(task_name)
        task_defs.append(
            _validate_task_spec(
                task_name,
                spec_dict,
                default_start_comment=start_comment,
            )
        )

    return task_defs


def _validate_task_spec(
    name: str,
    spec: Mapping[str, Any],
    *,
    default_start_comment: str | None,
) -> tuple[str, str, dict[str, Any]]:
    kwargs = dict(spec)
    task_type = kwargs.pop("type", None)
    if task_type is None:
        raise ValueError(f"Task {name!r} must define a type.")
    if not isinstance(task_type, str) or task_type not in _SUPPORTED_TASK_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_TASK_TYPES))
        raise ValueError(
            f"Task {name!r} has unsupported type {task_type!r}. "
            f"Expected one of: {supported}."
        )
    if task_type == _PIPELINE_TASK_TYPE:
        kwargs = _validate_pipeline_task(name, kwargs)
    else:
        _validate_public_task_argument_names(name, task_type, kwargs)
        effective_start_comment = default_start_comment
        if "start_comment" in kwargs:
            effective_start_comment = _validate_start_comment(
                f"Task {name!r} start_comment",
                kwargs.pop("start_comment"),
            )
        kwargs = _apply_start_comment(task_type, kwargs, effective_start_comment)
    return name, task_type, kwargs


def _validate_public_task_argument_names(
    name: str,
    task_type: str,
    kwargs: Mapping[str, Any],
) -> None:
    forbidden_fields = sorted(_FORBIDDEN_TASK_ARGUMENTS.intersection(kwargs))
    if not forbidden_fields:
        return

    fields = ", ".join(forbidden_fields)
    replacement = "from_db and to_db" if task_type == "transfer" else "db_key"
    raise ValueError(
        f"Task {name!r} ({task_type}) has unsupported SQL task argument(s): "
        f"{fields}. Use {replacement} instead."
    )


def _validate_start_comment(field_name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or None.")
    if not value.strip():
        return None
    return value


def _apply_start_comment(
    task_type: str,
    kwargs: dict[str, Any],
    start_comment: str | None,
) -> dict[str, Any]:
    if start_comment is None:
        return kwargs

    sql_field = _START_COMMENT_SQL_FIELDS.get(task_type)
    if sql_field is None:
        return kwargs

    query = kwargs.get(sql_field)
    if isinstance(query, str):
        kwargs[sql_field] = f"{start_comment.rstrip()}\n{query}"
    return kwargs


def _validate_pipeline_task(name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    extra_fields = sorted(set(kwargs) - {"steps"})
    if extra_fields:
        fields = ", ".join(extra_fields)
        raise ValueError(
            f"Task {name!r} has unsupported custom_sql_pipeline field(s): {fields}."
        )

    if "steps" not in kwargs:
        raise ValueError(f"Task {name!r} must define steps.")

    steps = kwargs["steps"]
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
        raise TypeError(f"Task {name!r} steps must be a non-empty sequence.")
    if not steps:
        raise ValueError(f"Task {name!r} steps must be a non-empty sequence.")

    steps_list = list(steps)
    for index, step in enumerate(steps_list):
        if not callable(step):
            raise TypeError(f"Task {name!r} step {index} must be callable.")

    return {"steps": steps_list}


def _validate_concurrency(concurrency: int) -> None:
    if concurrency.__class__ is not int or concurrency < 1:
        raise ValueError("concurrency must be an integer >= 1.")


def _validate_optional_soft_concurrency_cap(
    soft_concurrency_cap: int | None,
) -> None:
    if soft_concurrency_cap is None:
        return
    if soft_concurrency_cap.__class__ is not int or soft_concurrency_cap < 1:
        raise ValueError("soft_concurrency_cap must be an integer >= 1.")


def _validate_hard_concurrency_cap(hard_concurrency_cap: int) -> None:
    if hard_concurrency_cap.__class__ is not int or hard_concurrency_cap < 1:
        raise ValueError("hard_concurrency_cap must be an integer >= 1.")


def _validate_progress(progress: bool) -> None:
    validate_progress_option(progress)


def _run_sync_task(
    task_type: str,
    kwargs: dict[str, Any],
    *,
    task_runners: Mapping[str, Callable[[dict[str, Any]], Any]],
) -> Any:
    task_kwargs = dict(kwargs)
    if task_type in _PROGRESS_TASK_TYPES:
        if "progress" in task_kwargs:
            _validate_progress(task_kwargs["progress"])
        task_kwargs["progress"] = False

    task_runner = task_runners.get(task_type)
    if task_runner is not None:
        return task_runner(task_kwargs)
    raise ValueError(f"Unsupported task type: {task_type!r}.")

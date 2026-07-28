from __future__ import annotations

import contextvars
import inspect
from collections.abc import Mapping
from contextlib import ExitStack
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Semaphore
from typing import Any

import pandas as pd
from pandas.api.types import is_scalar
from tqdm import tqdm

from analytics_toolkit import sql as sql_facade
from analytics_toolkit.general import time_print

from .api import _compute_test_metrics_dataframe

async_sql = sql_facade.async_sql

_SQL_DATAFRAME_FIELDS = frozenset({"df", "pre_exp_df", "pre_exp_metrics_df"})
_COMPUTE_TEST_METRICS_FIELDS = frozenset(
    inspect.signature(_compute_test_metrics_dataframe).parameters
)
_DEFAULT_HARD_CONCURRENCY_CAP = 5
_CONCURRENCY_STATE: contextvars.ContextVar["_ConcurrencyState | None"] = (
    contextvars.ContextVar("analytics_toolkit_ab_parallel_concurrency", default=None)
)
_MISSING = object()


@dataclass(frozen=True)
class _ConcurrencyState:
    effective_concurrency: int
    hard_cap: int
    soft_cap: int
    semaphores: tuple[Semaphore, ...]


def _shutdown_executor(
    executor: ThreadPoolExecutor,
    *,
    wait: bool,
    cancel_futures: bool,
) -> None:
    try:
        executor.shutdown(wait=wait, cancel_futures=cancel_futures)
    except TypeError:
        executor.shutdown(wait=wait)


def _compute_metric_tasks(
    tasks: Mapping[str, Mapping[str, Any]],
    *,
    metric_defaults: Mapping[str, Any] | None = None,
    concurrency: int = 1,
    fail_fast: bool = True,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = _DEFAULT_HARD_CONCURRENCY_CAP,
    progress: bool = False,
) -> dict[str, pd.DataFrame | str]:
    """Run independent ``compute_test_metrics`` tasks concurrently."""
    defaults = _validate_metric_task_defaults(metric_defaults or {})
    task_defs = _validate_tasks(tasks, metric_defaults=defaults)
    _validate_concurrency(concurrency)
    _validate_optional_soft_concurrency_cap(soft_concurrency_cap)
    _validate_hard_concurrency_cap(hard_concurrency_cap)
    _validate_progress(progress)
    state = _build_concurrency_state(
        concurrency=concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
    )
    reset_token = _CONCURRENCY_STATE.set(state)

    results_by_index: dict[int, pd.DataFrame | str] = {}
    executor: ThreadPoolExecutor | None = None
    shutdown_called = False
    progress_bar: Any | None = None

    try:
        executor = ThreadPoolExecutor(max_workers=min(concurrency, state.soft_cap))
        progress_bar = _make_progress_bar(total=len(task_defs), progress=progress)
        future_to_index: dict[Future[pd.DataFrame], int] = {
            executor.submit(
                contextvars.copy_context().run,
                _run_task_with_concurrency_state,
                state.semaphores,
                kwargs,
                labels,
            ): index
            for index, (_name, kwargs, labels) in enumerate(task_defs)
        }

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results_by_index[index] = future.result()
            except BaseException as exc:
                _annotate_metric_exception(exc, task_defs[index][0])
                if fail_fast:
                    for pending in future_to_index:
                        if pending is not future:
                            pending.cancel()
                    _shutdown_executor(executor, wait=False, cancel_futures=True)
                    shutdown_called = True
                    raise
                results_by_index[index] = str(exc)
            finally:
                progress_bar.update(1)

        return {
            name: results_by_index[index]
            for index, (name, _kwargs, _labels) in enumerate(task_defs)
        }
    finally:
        if progress_bar is not None:
            progress_bar.close()
        if executor is not None and not shutdown_called:
            _shutdown_executor(executor, wait=True, cancel_futures=True)
        _CONCURRENCY_STATE.reset(reset_token)


def compute_metrics_from_sql(
    tasks: Mapping[str, Mapping[str, Any]],
    db_key: str,
    *,
    concurrency: int = 1,
    fail_fast: bool = True,
    start_comment: str | None = None,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = _DEFAULT_HARD_CONCURRENCY_CAP,
    progress: bool = False,
    **metric_defaults: Any,
) -> dict[str, pd.DataFrame | str]:
    """Load SQL-backed task dataframes, then run ``compute_test_metrics``."""
    _validate_start_comment("start_comment", start_comment)
    metric_defaults = _validate_metric_defaults(metric_defaults)
    task_defs = _validate_sql_tasks(tasks)
    _validate_concurrency(concurrency)
    _validate_optional_soft_concurrency_cap(soft_concurrency_cap)
    _validate_hard_concurrency_cap(hard_concurrency_cap)
    _validate_progress(progress)

    sql_tasks: list[dict[str, Any]] = []
    for name, kwargs, sql, pre_exp_sql, task_start_comment in task_defs:
        sql_tasks.append(
            _make_sql_read_task(
                name=name,
                field="sql",
                db_key=db_key,
                query=sql,
                start_comment=task_start_comment,
            )
        )
        if pre_exp_sql is not None:
            sql_tasks.append(
                _make_sql_read_task(
                    name=name,
                    field="pre_exp_sql",
                    db_key=db_key,
                    query=pre_exp_sql,
                    start_comment=task_start_comment,
                )
            )

    async_kwargs: dict[str, Any] = {
        "concurrency": concurrency,
        "fail_fast": fail_fast,
        "progress": progress,
    }
    if start_comment is not None:
        async_kwargs["start_comment"] = start_comment
    if soft_concurrency_cap is not None:
        async_kwargs["soft_concurrency_cap"] = soft_concurrency_cap
    if hard_concurrency_cap != _DEFAULT_HARD_CONCURRENCY_CAP:
        async_kwargs["hard_concurrency_cap"] = hard_concurrency_cap

    try:
        sql_results = async_sql(sql_tasks, **async_kwargs)
    except BaseException as exc:
        _log_sql_metric_task_failure_from_exception(exc, task_defs)
        raise

    metric_tasks: dict[str, dict[str, Any]] = {}
    sql_failures: dict[str, str] = {}
    for name, kwargs, _sql, pre_exp_sql, _task_start_comment in task_defs:
        df_result = sql_results[_sql_read_task_name(name, "sql")]
        if isinstance(df_result, str):
            _log_sql_metric_task_failure(
                name=name,
                failed_field="sql",
                error=df_result,
                sql=_sql,
                pre_exp_sql=pre_exp_sql,
            )
            sql_failures[name] = df_result
            continue

        metric_kwargs = dict(metric_defaults)
        metric_kwargs.update(kwargs)
        metric_kwargs["df"] = df_result
        if pre_exp_sql is not None:
            pre_exp_df_result = sql_results[_sql_read_task_name(name, "pre_exp_sql")]
            if isinstance(pre_exp_df_result, str):
                _log_sql_metric_task_failure(
                    name=name,
                    failed_field="pre_exp_sql",
                    error=pre_exp_df_result,
                    sql=_sql,
                    pre_exp_sql=pre_exp_sql,
                )
                sql_failures[name] = pre_exp_df_result
                continue
            metric_kwargs["pre_exp_df"] = pre_exp_df_result
        metric_tasks[name] = metric_kwargs

    metric_kwargs: dict[str, Any] = {
        "concurrency": concurrency,
        "fail_fast": fail_fast,
        "progress": progress,
    }
    if soft_concurrency_cap is not None:
        metric_kwargs["soft_concurrency_cap"] = soft_concurrency_cap
    if hard_concurrency_cap != _DEFAULT_HARD_CONCURRENCY_CAP:
        metric_kwargs["hard_concurrency_cap"] = hard_concurrency_cap

    try:
        metric_results = (
            _compute_metric_tasks(metric_tasks, metric_defaults={}, **metric_kwargs)
            if metric_tasks
            else {}
        )
    except BaseException as exc:
        _log_sql_metric_compute_failure_from_exception(exc, task_defs)
        raise

    results: dict[str, pd.DataFrame | str] = {}
    for name, _kwargs, sql, pre_exp_sql, _task_start_comment in task_defs:
        if name in sql_failures:
            results[name] = sql_failures[name]
            continue

        metric_result = metric_results[name]
        if isinstance(metric_result, str):
            _log_sql_metric_compute_failure(
                name=name,
                error=metric_result,
                sql=sql,
                pre_exp_sql=pre_exp_sql,
            )
        results[name] = metric_result
    return results


def _make_progress_bar(*, total: int, progress: bool) -> Any:
    return tqdm(
        total=total,
        desc="compute_test_metrics tasks",
        unit="task",
        disable=not progress,
    )


def _build_concurrency_state(
    *,
    concurrency: int,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
) -> _ConcurrencyState:
    active_state = _CONCURRENCY_STATE.get()
    if active_state is None:
        hard_cap = hard_concurrency_cap
        soft_cap = concurrency if soft_concurrency_cap is None else soft_concurrency_cap
        semaphores = (Semaphore(soft_cap),)
        effective_concurrency = concurrency
    else:
        hard_cap = active_state.hard_cap
        if (
            hard_concurrency_cap != _DEFAULT_HARD_CONCURRENCY_CAP
            and hard_concurrency_cap >= hard_cap
        ):
            hard_cap = hard_concurrency_cap

        soft_cap = active_state.soft_cap
        semaphores = active_state.semaphores
        if soft_concurrency_cap is not None and soft_concurrency_cap < soft_cap:
            soft_cap = soft_concurrency_cap
            semaphores = (*semaphores, Semaphore(soft_cap))

        effective_concurrency = active_state.effective_concurrency * concurrency

    actual_worker_ceiling = min(effective_concurrency, soft_cap)
    if actual_worker_ceiling > hard_cap:
        raise ValueError(
            "effective concurrency exceeds hard_concurrency_cap "
            f"({actual_worker_ceiling} > {hard_cap}). Reduce concurrency, set "
            "soft_concurrency_cap at or below hard_concurrency_cap, or increase "
            "hard_concurrency_cap."
        )

    return _ConcurrencyState(
        effective_concurrency=effective_concurrency,
        hard_cap=hard_cap,
        soft_cap=soft_cap,
        semaphores=semaphores,
    )


def _run_task_with_concurrency_state(
    semaphores: tuple[Semaphore, ...],
    kwargs: dict[str, Any],
    labels: dict[str, Any],
) -> pd.DataFrame:
    with ExitStack() as stack:
        for semaphore in reversed(semaphores):
            stack.enter_context(semaphore)
        return _run_task(kwargs, labels)


def _run_task(kwargs: dict[str, Any], labels: dict[str, Any]) -> pd.DataFrame:
    result = _compute_test_metrics_dataframe(**kwargs)
    if not labels:
        return result

    labeled_result = result.copy()
    conflicts = [column for column in labels if column in labeled_result.columns]
    if conflicts:
        fields = ", ".join(conflicts)
        raise ValueError(f"Label column(s) conflict with result columns: {fields}.")

    for index, (column, value) in enumerate(labels.items()):
        labeled_result.insert(index, column, value)
    return labeled_result


def _annotate_metric_exception(exc: BaseException, name: str) -> None:
    try:
        setattr(exc, "analytics_toolkit_metric_task_name", name)
    except Exception:
        return


def _validate_tasks(
    tasks: Mapping[str, Mapping[str, Any]],
    *,
    metric_defaults: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    if not isinstance(tasks, Mapping):
        raise TypeError("tasks must be a non-empty mapping of task names to task mappings.")
    if not tasks:
        raise ValueError("tasks must be a non-empty mapping.")

    task_defs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for name, spec in tasks.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Task names must be non-empty strings.")
        if not isinstance(spec, Mapping):
            raise TypeError(f"Task {name!r} must be a mapping.")
        task_defs.append(_validate_task_spec(name, spec, metric_defaults=metric_defaults))
    return task_defs


def _validate_task_spec(
    name: str,
    spec: Mapping[str, Any],
    *,
    metric_defaults: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    kwargs = dict(metric_defaults)
    kwargs.update(spec)
    if "df" not in kwargs:
        raise ValueError(f"Task {name!r} must define df.")

    labels = _validate_labels(name, kwargs.pop("labels", None))
    pre_exp_df = kwargs.pop("pre_exp_df", None)
    if pre_exp_df is not None:
        if "pre_exp_metrics_df" in kwargs:
            raise ValueError(
                f"Task {name!r} cannot define both pre_exp_df and pre_exp_metrics_df."
            )
        kwargs["pre_exp_metrics_df"] = pre_exp_df

    return name, kwargs, labels


def _validate_sql_tasks(
    tasks: Mapping[str, Mapping[str, Any]],
) -> list[tuple[str, dict[str, Any], str, str | None, Any]]:
    if not isinstance(tasks, Mapping):
        raise TypeError("tasks must be a non-empty mapping of task names to task mappings.")
    if not tasks:
        raise ValueError("tasks must be a non-empty mapping.")

    task_defs: list[tuple[str, dict[str, Any], str, str | None, Any]] = []
    for name, spec in tasks.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Task names must be non-empty strings.")
        if not isinstance(spec, Mapping):
            raise TypeError(f"Task {name!r} must be a mapping.")
        task_defs.append(_validate_sql_task_spec(name, spec))
    return task_defs


def _validate_sql_task_spec(
    name: str,
    spec: Mapping[str, Any],
) -> tuple[str, dict[str, Any], str, str | None, Any]:
    kwargs = dict(spec)
    ambiguous_fields = sorted(_SQL_DATAFRAME_FIELDS.intersection(kwargs))
    if ambiguous_fields:
        fields = ", ".join(ambiguous_fields)
        raise ValueError(
            f"Task {name!r} cannot define dataframe field(s) for SQL-backed "
            f"inputs: {fields}."
        )

    if "sql" not in kwargs:
        raise ValueError(f"Task {name!r} must define sql.")
    sql = kwargs.pop("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError(f"Task {name!r} sql must be a non-empty string.")

    pre_exp_sql = kwargs.pop("pre_exp_sql", None)
    if pre_exp_sql is not None and (
        not isinstance(pre_exp_sql, str) or not pre_exp_sql.strip()
    ):
        raise ValueError(f"Task {name!r} pre_exp_sql must be a non-empty string.")

    start_comment: Any = _MISSING
    if "start_comment" in kwargs:
        start_comment = kwargs.pop("start_comment")
        _validate_start_comment(f"Task {name!r} start_comment", start_comment)

    _validate_labels(name, kwargs.get("labels"))
    return name, kwargs, sql, pre_exp_sql, start_comment


def _validate_metric_defaults(metric_defaults: Mapping[str, Any]) -> dict[str, Any]:
    if not metric_defaults:
        return {}

    dataframe_fields = sorted(_SQL_DATAFRAME_FIELDS.intersection(metric_defaults))
    if dataframe_fields:
        fields = ", ".join(dataframe_fields)
        raise ValueError(
            "compute_metrics_from_sql top-level metric defaults cannot "
            f"define SQL-backed dataframe field(s): {fields}."
        )

    supported_fields = _COMPUTE_TEST_METRICS_FIELDS - {
        "df",
        "pre_exp_metrics_df",
    }
    unexpected_fields = sorted(set(metric_defaults) - supported_fields)
    if unexpected_fields:
        quoted_fields = ", ".join(repr(field) for field in unexpected_fields)
        if len(unexpected_fields) == 1:
            raise TypeError(
                "compute_metrics_from_sql() got an unexpected keyword "
                f"argument {quoted_fields}"
            )
        raise TypeError(
            "compute_metrics_from_sql() got unexpected keyword "
            f"arguments: {quoted_fields}"
        )

    return dict(metric_defaults)


def _validate_metric_task_defaults(metric_defaults: Mapping[str, Any]) -> dict[str, Any]:
    supported_fields = _COMPUTE_TEST_METRICS_FIELDS - {"df"}
    unexpected_fields = sorted(set(metric_defaults) - supported_fields)
    if unexpected_fields:
        quoted_fields = ", ".join(repr(field) for field in unexpected_fields)
        if len(unexpected_fields) == 1:
            raise TypeError(
                "compute_test_metrics() got an unexpected task default "
                f"{quoted_fields}"
            )
        raise TypeError(
            "compute_test_metrics() got unexpected task defaults: "
            f"{quoted_fields}"
        )
    return dict(metric_defaults)


def _make_sql_read_task(
    *,
    name: str,
    field: str,
    db_key: str,
    query: str,
    start_comment: Any,
) -> dict[str, Any]:
    task = {
        "name": _sql_read_task_name(name, field),
        "type": "read",
        "db_key": db_key,
        "query": query,
    }
    if start_comment is not _MISSING:
        task["start_comment"] = start_comment
    return task


def _validate_start_comment(field_name: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or None.")


def _sql_read_task_name(name: str, field: str) -> str:
    return f"{name}:{field}"


def _log_sql_metric_task_failure_from_exception(
    exc: BaseException,
    task_defs: list[tuple[str, dict[str, Any], str, str | None, Any]],
) -> None:
    sql_task_name = getattr(exc, "analytics_toolkit_sql_task_name", None)
    failed_field = _sql_read_task_field(sql_task_name)
    if failed_field is None:
        return

    metric_task_name = sql_task_name[: -len(f":{failed_field}")]
    for name, _kwargs, sql, pre_exp_sql, _task_start_comment in task_defs:
        if name == metric_task_name:
            _log_sql_metric_task_failure(
                name=name,
                failed_field=failed_field,
                error=str(exc),
                sql=sql,
                pre_exp_sql=pre_exp_sql,
            )
            return


def _log_sql_metric_compute_failure_from_exception(
    exc: BaseException,
    task_defs: list[tuple[str, dict[str, Any], str, str | None, Any]],
) -> None:
    metric_task_name = getattr(exc, "analytics_toolkit_metric_task_name", None)
    if not isinstance(metric_task_name, str):
        return

    for name, _kwargs, sql, pre_exp_sql, _task_start_comment in task_defs:
        if name == metric_task_name:
            _log_sql_metric_compute_failure(
                name=name,
                error=str(exc),
                sql=sql,
                pre_exp_sql=pre_exp_sql,
            )
            return


def _sql_read_task_field(sql_task_name: Any) -> str | None:
    if not isinstance(sql_task_name, str):
        return None
    for field in ("pre_exp_sql", "sql"):
        if sql_task_name.endswith(f":{field}"):
            return field
    return None


def _log_sql_metric_task_failure(
    *,
    name: str,
    failed_field: str,
    error: str,
    sql: str,
    pre_exp_sql: str | None,
) -> None:
    pre_exp_message = (
        pre_exp_sql
        if pre_exp_sql is not None
        else "pre_exp_sql was not provided for this metrics task."
    )
    time_print(
        f"compute_metrics_from_sql task {name!r} failed while loading "
        f"{failed_field}: {error}\n"
        f"Experiment SQL:\n{sql}\n"
        f"Pre-experiment SQL:\n{pre_exp_message}"
    )


def _log_sql_metric_compute_failure(
    *,
    name: str,
    error: str,
    sql: str,
    pre_exp_sql: str | None,
) -> None:
    pre_exp_message = (
        pre_exp_sql
        if pre_exp_sql is not None
        else "pre_exp_sql was not provided for this metrics task."
    )
    time_print(
        f"compute_metrics_from_sql task {name!r} failed during metric "
        f"computation: {error}\n"
        f"Experiment SQL:\n{sql}\n"
        f"Pre-experiment SQL:\n{pre_exp_message}"
    )


def _validate_labels(name: str, labels: Any) -> dict[str, Any]:
    if labels is None:
        return {}
    if not isinstance(labels, Mapping):
        raise TypeError(f"Task {name!r} labels must be a mapping.")

    labels_dict = dict(labels)
    for column, value in labels_dict.items():
        if not isinstance(column, str) or not column.strip():
            raise ValueError(f"Task {name!r} label columns must be non-empty strings.")
        if not is_scalar(value):
            raise ValueError(f"Task {name!r} label {column!r} must be a scalar value.")
    return labels_dict


def _validate_concurrency(concurrency: int) -> None:
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or concurrency < 1
    ):
        raise ValueError("concurrency must be an integer >= 1.")


def _validate_optional_soft_concurrency_cap(
    soft_concurrency_cap: int | None,
) -> None:
    if soft_concurrency_cap is None:
        return
    if (
        not isinstance(soft_concurrency_cap, int)
        or isinstance(soft_concurrency_cap, bool)
        or soft_concurrency_cap < 1
    ):
        raise ValueError("soft_concurrency_cap must be an integer >= 1.")


def _validate_hard_concurrency_cap(hard_concurrency_cap: int) -> None:
    if (
        not isinstance(hard_concurrency_cap, int)
        or isinstance(hard_concurrency_cap, bool)
        or hard_concurrency_cap < 1
    ):
        raise ValueError("hard_concurrency_cap must be an integer >= 1.")


def _validate_progress(progress: bool) -> None:
    if not isinstance(progress, bool):
        raise ValueError("progress must be a boolean.")

from __future__ import annotations

import contextvars
import inspect
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from threading import Semaphore
from typing import Any

from tqdm import tqdm

from ..dml.io.execute_read import execute_read
from ..dml.io.execute_sql import execute_sql
from ..dml.io.read_sql import read_sql
from ..dml.load.load_df import load_df
from ..dml.transfer.flow.api import transfer_table
from ..execution.operation_runner import timed_public_sql_function
from .pipeline import _PipelineContext, _is_async_callable
from .tasks import (
    _PIPELINE_TASK_TYPE,
    _PROGRESS_TASK_TYPES,
    _annotate_task_exception,
    _log_failed_sql_task,
    _normalize_task_result,
    _run_sync_task as _dispatch_sync_task,
    _validate_concurrency,
    _validate_hard_concurrency_cap,
    _validate_optional_soft_concurrency_cap,
    _validate_progress,
    _validate_tasks,
)

_DEFAULT_HARD_CONCURRENCY_CAP = 10
_SYNC_TASK_RUNNERS = {
    "read": lambda kwargs: read_sql(**kwargs),
    "execute": lambda kwargs: execute_sql(**kwargs),
    "execute_read": lambda kwargs: execute_read(**kwargs),
    "load_df": lambda kwargs: load_df(**kwargs),
    "transfer": lambda kwargs: transfer_table(**kwargs),
}
_PARALLEL_CONCURRENCY_STATE: contextvars.ContextVar[
    "_ParallelConcurrencyState | None"
] = contextvars.ContextVar("analytics_toolkit_parallel_sql_concurrency", default=None)
_PARALLEL_HELD_SEMAPHORES: contextvars.ContextVar[tuple[Semaphore, ...]] = (
    contextvars.ContextVar("analytics_toolkit_parallel_sql_held_semaphores", default=())
)


@dataclass(frozen=True)
class _ParallelConcurrencyState:
    effective_concurrency: int
    hard_cap: int
    soft_cap: int
    semaphores: tuple[Semaphore, ...]


@timed_public_sql_function
def parallel_sql(
    tasks: Sequence[Mapping[str, Any]],
    *,
    concurrency: int = 5,
    fail_fast: bool = True,
    start_comment: str | None = None,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = _DEFAULT_HARD_CONCURRENCY_CAP,
    progress: bool = True,
) -> dict[str, Any]:
    """Run independent SQL tasks concurrently using worker threads."""
    task_defs = _validate_tasks(tasks, start_comment=start_comment)
    _validate_concurrency(concurrency)
    _validate_optional_soft_concurrency_cap(soft_concurrency_cap)
    _validate_hard_concurrency_cap(hard_concurrency_cap)
    _validate_progress(progress)
    state = _build_parallel_concurrency_state(
        concurrency=concurrency,
        soft_concurrency_cap=soft_concurrency_cap,
        hard_concurrency_cap=hard_concurrency_cap,
    )
    reset_token = _PARALLEL_CONCURRENCY_STATE.set(state)
    try:
        with _release_held_parallel_semaphores():
            return _run_parallel_task_defs(
                task_defs,
                concurrency=concurrency,
                fail_fast=fail_fast,
                progress=progress,
                soft_semaphores=state.semaphores,
            )
    finally:
        _PARALLEL_CONCURRENCY_STATE.reset(reset_token)


def _run_parallel_task_defs(
    task_defs: list[tuple[str, str, dict[str, Any]]],
    *,
    concurrency: int,
    fail_fast: bool,
    progress: bool,
    soft_semaphores: tuple[Semaphore, ...],
) -> dict[str, Any]:
    progress_bar = _make_progress_bar(total=len(task_defs), progress=progress)
    executor = ThreadPoolExecutor(max_workers=concurrency)
    executor_shutdown = False

    try:
        future_to_index: dict[Future[tuple[int, Any]], int] = {}
        for index, (name, task_type, kwargs) in enumerate(task_defs):
            context = contextvars.copy_context()
            future = executor.submit(
                context.run,
                _run_parallel_indexed,
                index,
                name,
                task_type,
                kwargs,
                soft_semaphores,
            )
            future_to_index[future] = index

        results_by_index: dict[int, Any] = {}
        if fail_fast:
            try:
                for future in as_completed(future_to_index):
                    try:
                        index, result = future.result()
                    finally:
                        progress_bar.update(1)
                    results_by_index[index] = _normalize_task_result(result)
            except BaseException:
                for pending in future_to_index:
                    pending.cancel()
                _shutdown_executor(executor, wait=True, cancel_futures=True)
                executor_shutdown = True
                raise
        else:
            for future in as_completed(future_to_index):
                default_index = future_to_index[future]
                try:
                    index, result = future.result()
                except BaseException as exc:
                    results_by_index[default_index] = str(exc)
                else:
                    results_by_index[index] = _normalize_task_result(result)
                finally:
                    progress_bar.update(1)

        return {
            name: results_by_index[index]
            for index, (name, _task_type, _kwargs) in enumerate(task_defs)
        }
    finally:
        progress_bar.close()
        if not executor_shutdown:
            executor.shutdown(wait=True)


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


def _make_progress_bar(*, total: int, progress: bool) -> Any:
    return tqdm(
        total=total,
        desc="parallel_sql tasks",
        unit="task",
        disable=not progress,
    )


def _build_parallel_concurrency_state(
    *,
    concurrency: int,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
) -> _ParallelConcurrencyState:
    active_state = _PARALLEL_CONCURRENCY_STATE.get()
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

    return _ParallelConcurrencyState(
        effective_concurrency=effective_concurrency,
        hard_cap=hard_cap,
        soft_cap=soft_cap,
        semaphores=semaphores,
    )


def _run_parallel_indexed(
    index: int,
    name: str,
    task_type: str,
    kwargs: dict[str, Any],
    soft_semaphores: tuple[Semaphore, ...],
) -> tuple[int, Any]:
    try:
        return index, _run_parallel_task(name, task_type, kwargs, soft_semaphores)
    except BaseException as exc:
        _annotate_task_exception(exc, name, task_type, kwargs)
        _log_failed_sql_task(name, task_type, kwargs, exc)
        raise


def _run_parallel_task(
    name: str,
    task_type: str,
    kwargs: dict[str, Any],
    soft_semaphores: tuple[Semaphore, ...],
) -> Any:
    if task_type == _PIPELINE_TASK_TYPE:
        return _run_parallel_pipeline(name, kwargs["steps"], soft_semaphores)
    return _run_with_thread_semaphores(
        soft_semaphores,
        _run_sync_task,
        task_type,
        kwargs,
    )


def _run_parallel_pipeline(
    task_name: str,
    steps: Sequence[Any],
    soft_semaphores: tuple[Semaphore, ...],
) -> Any:
    context = _PipelineContext(task_name=task_name)
    for index, step in enumerate(steps):
        context.step_index = index
        if _is_async_callable(step):
            raise TypeError(
                "parallel_sql does not support async custom_sql_pipeline steps; "
                "use async_sql for async pipeline steps."
            )
        result = _run_with_thread_semaphores(soft_semaphores, step, context)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise TypeError(
                "parallel_sql does not support coroutine custom_sql_pipeline "
                "step results; use async_sql for async pipeline steps."
            )
        context.results.append(result)

    return context.last_result


def _run_with_thread_semaphores(
    soft_semaphores: tuple[Semaphore, ...],
    func: Any,
    *args: Any,
) -> Any:
    with ExitStack() as stack:
        acquired: list[Semaphore] = []
        for semaphore in reversed(soft_semaphores):
            semaphore.acquire()
            acquired.append(semaphore)
            stack.callback(semaphore.release)
        token = _PARALLEL_HELD_SEMAPHORES.set(
            (*_PARALLEL_HELD_SEMAPHORES.get(), *acquired)
        )
        try:
            return func(*args)
        finally:
            _PARALLEL_HELD_SEMAPHORES.reset(token)


@contextmanager
def _release_held_parallel_semaphores() -> Any:
    held_semaphores = _PARALLEL_HELD_SEMAPHORES.get()
    if not held_semaphores:
        yield
        return

    token = _PARALLEL_HELD_SEMAPHORES.set(())
    for semaphore in held_semaphores:
        semaphore.release()
    try:
        yield
    finally:
        for semaphore in held_semaphores:
            semaphore.acquire()
        _PARALLEL_HELD_SEMAPHORES.reset(token)


def _run_sync_task(task_type: str, kwargs: dict[str, Any]) -> Any:
    return _dispatch_sync_task(
        task_type,
        kwargs,
        task_runners=_SYNC_TASK_RUNNERS,
    )

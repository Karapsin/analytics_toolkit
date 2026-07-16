from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from dataclasses import dataclass
from functools import partial
from queue import Queue
from threading import Event, Thread
from typing import Any

from tqdm import tqdm

from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
    cancel_scope_queries,
    current_cancellation_scope,
    raise_if_cancelled,
    shutdown_executor,
)

from ..dml.io.execute_read import execute_read
from ..dml.io.execute_sql import execute_sql
from ..dml.io.read_sql import read_sql
from ..dml.load.load_df import load_df
from ..dml.transfer.flow.api import transfer_table
from ..execution.operation_runner import timed_public_sql_function
from .pipeline import _PipelineContext, _is_async_callable
from analytics_toolkit.general import time_print_context
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
_CONCURRENCY_STATE: contextvars.ContextVar["_ConcurrencyState | None"] = contextvars.ContextVar(
    "analytics_toolkit_async_sql_concurrency", default=None
)


@dataclass(frozen=True)
class _ConcurrencyState:
    effective_concurrency: int
    hard_cap: int
    soft_cap: int
    semaphores: tuple[asyncio.Semaphore, ...]


@timed_public_sql_function
def async_sql(
    tasks: Sequence[Mapping[str, Any]],
    *,
    concurrency: int = 5,
    fail_fast: bool = True,
    start_comment: str | None = None,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = _DEFAULT_HARD_CONCURRENCY_CAP,
    progress: bool = False,
) -> dict[str, Any]:
    """Run independent SQL tasks concurrently and return a result dictionary."""
    cancellation_scope = SqlCancellationScope(parent=current_cancellation_scope())
    with activate_cancellation_scope(cancellation_scope):
        return _run_coroutine_sync(
            lambda: _async_sql_impl(
                tasks,
                concurrency=concurrency,
                fail_fast=fail_fast,
                start_comment=start_comment,
                soft_concurrency_cap=soft_concurrency_cap,
                hard_concurrency_cap=hard_concurrency_cap,
                progress=progress,
            ),
            cancellation_scope=cancellation_scope,
        )


async def _async_sql_impl(
    tasks: Sequence[Mapping[str, Any]],
    *,
    concurrency: int = 5,
    fail_fast: bool = True,
    start_comment: str | None = None,
    soft_concurrency_cap: int | None = None,
    hard_concurrency_cap: int = _DEFAULT_HARD_CONCURRENCY_CAP,
    progress: bool = False,
) -> dict[str, Any]:
    scope = current_cancellation_scope() or SqlCancellationScope()
    with activate_cancellation_scope(scope):
        return await _async_sql_impl_scoped(
            tasks,
            concurrency=concurrency,
            fail_fast=fail_fast,
            start_comment=start_comment,
            soft_concurrency_cap=soft_concurrency_cap,
            hard_concurrency_cap=hard_concurrency_cap,
            progress=progress,
            cancellation_scope=scope,
        )


async def _async_sql_impl_scoped(  # noqa: PLR0912, PLR0913, PLR0915
    tasks: Sequence[Mapping[str, Any]],
    *,
    concurrency: int,
    fail_fast: bool,
    start_comment: str | None,
    soft_concurrency_cap: int | None,
    hard_concurrency_cap: int,
    progress: bool,
    cancellation_scope: SqlCancellationScope,
) -> dict[str, Any]:
    task_defs = _validate_tasks(tasks, start_comment=start_comment)
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
    executor = ThreadPoolExecutor(
        max_workers=max(1, min(concurrency, state.soft_cap)),
        thread_name_prefix="async-sql-worker",
    )
    cancellation_scope.register_executor(executor)

    semaphore = asyncio.Semaphore(concurrency)

    async def run_task(name: str, task_type: str, kwargs: dict[str, Any]) -> Any:
        async with semaphore:
            if task_type == _PIPELINE_TASK_TYPE:
                return await _run_pipeline(
                    name,
                    kwargs["steps"],
                    state.semaphores,
                    executor,
                )
            return await _run_blocking(
                state.semaphores,
                executor,
                _run_sync_task,
                task_type,
                kwargs,
            )

    async def run_indexed(
        index: int,
        name: str,
        task_type: str,
        kwargs: dict[str, Any],
    ) -> tuple[int, Any]:
        with time_print_context(task_id=name):
            try:
                return index, await run_task(name, task_type, kwargs)
            except BaseException as exc:
                _annotate_task_exception(exc, name, task_type, kwargs)
                _log_failed_sql_task(name, task_type, kwargs, exc)
                raise

    try:
        async_tasks = [
            asyncio.create_task(run_indexed(index, name, task_type, kwargs))
            for index, (name, task_type, kwargs) in enumerate(task_defs)
        ]
        progress_bar = _make_progress_bar(
            total=len(async_tasks),
            progress=progress,
        )
        for task in async_tasks:
            task.add_done_callback(lambda _task: progress_bar.update(1))

        try:
            if fail_fast:
                results_by_index: dict[int, Any] = {}
                for finished in asyncio.as_completed(async_tasks):
                    index, result = await finished
                    results_by_index[index] = _normalize_task_result(result)

                return {
                    name: results_by_index[index]
                    for index, (name, _task_type, _kwargs) in enumerate(task_defs)
                }

            indexed_results = await asyncio.gather(
                *async_tasks,
                return_exceptions=True,
            )
            results_by_index: dict[int, Any] = {}
            for default_index, item in enumerate(indexed_results):
                _raise_interrupt_result(item)
                if isinstance(item, BaseException):
                    results_by_index[default_index] = str(item)
                else:
                    index, result = item
                    results_by_index[index] = _normalize_task_result(result)
            return {
                name: results_by_index[index]
                for index, (name, _task_type, _kwargs) in enumerate(task_defs)
            }
        except BaseException as exc:
            if _is_interrupt_exception(exc):
                cancellation_scope.request_cancel()
                for task in async_tasks:
                    if not task.done():
                        task.cancel()
                cancel_scope_queries(cancellation_scope)
            else:
                for task in async_tasks:
                    if not task.done():
                        task.cancel()
            await asyncio.gather(*async_tasks, return_exceptions=True)
            raise
    finally:
        if "progress_bar" in locals():
            progress_bar.close()
        cancellation_scope.unregister_executor(executor)
        shutdown_executor(
            executor,
            wait=not cancellation_scope.cancelled,
            cancel_futures=cancellation_scope.cancelled,
        )
        _CONCURRENCY_STATE.reset(reset_token)


def _run_coroutine_sync(
    coroutine_factory: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    *,
    cancellation_scope: SqlCancellationScope | None = None,
) -> dict[str, Any]:
    if _is_event_loop_running():
        return _run_coroutine_sync_in_thread(
            coroutine_factory,
            cancellation_scope=cancellation_scope,
        )
    return asyncio.run(coroutine_factory())


def _run_coroutine_sync_in_thread(
    coroutine_factory: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    *,
    cancellation_scope: SqlCancellationScope | None = None,
) -> dict[str, Any]:
    queue: Queue[tuple[bool, dict[str, Any] | BaseException, Any | None]] = Queue(maxsize=1)
    runner_ready = Event()
    runner_state: dict[str, Any] = {}

    async def run_captured() -> dict[str, Any]:
        runner_state["loop"] = asyncio.get_running_loop()
        runner_state["task"] = asyncio.current_task()
        runner_ready.set()
        return await coroutine_factory()

    def run() -> None:
        try:
            queue.put((True, asyncio.run(run_captured()), None))
        except BaseException as exc:
            queue.put((False, exc, exc.__traceback__))

    context = contextvars.copy_context()
    thread = Thread(target=lambda: context.run(run), daemon=True)
    thread.start()
    try:
        ok, value, traceback = queue.get()
    except BaseException:
        if cancellation_scope is not None:
            cancellation_scope.request_cancel()
        runner_ready.wait(timeout=1)
        loop = runner_state.get("loop")
        task = runner_state.get("task")
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout=10)
        raise
    thread.join()

    if ok:
        return value  # type: ignore[return-value]
    raise value.with_traceback(traceback)  # type: ignore[union-attr]


def _is_event_loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _make_progress_bar(*, total: int, progress: bool) -> Any:
    return tqdm(
        total=total,
        desc="async_sql tasks",
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
        semaphores = (asyncio.Semaphore(soft_cap),)
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
            semaphores = (*semaphores, asyncio.Semaphore(soft_cap))

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


async def _run_pipeline(
    task_name: str,
    steps: Sequence[Any],
    soft_semaphores: tuple[asyncio.Semaphore, ...],
    executor: ThreadPoolExecutor,
) -> Any:
    context = _PipelineContext(task_name=task_name)
    for index, step in enumerate(steps):
        context.step_index = index
        if _is_async_callable(step):
            result = await step(context)
        else:
            result = await _run_blocking(
                soft_semaphores,
                executor,
                step,
                context,
            )
        context.results.append(result)

    return context.last_result


async def _run_blocking(
    soft_semaphores: tuple[asyncio.Semaphore, ...],
    executor: ThreadPoolExecutor,
    func: Any,
    *args: Any,
) -> Any:
    async with AsyncExitStack() as stack:
        for semaphore in reversed(soft_semaphores):
            await stack.enter_async_context(semaphore)
        return await _to_thread(func, *args, _executor=executor)


async def _to_thread(
    func: Any,
    *args: Any,
    _executor: ThreadPoolExecutor | None = None,
    **kwargs: Any,
) -> Any:
    if _executor is not None:
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        call = partial(func, *args, **kwargs)

        def run_guarded() -> Any:
            raise_if_cancelled()
            return call()

        return await loop.run_in_executor(_executor, context.run, run_guarded)

    to_thread = getattr(asyncio, "to_thread", None)
    if to_thread is not None:
        return await to_thread(func, *args, **kwargs)

    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, context.run, call)


def _run_sync_task(task_type: str, kwargs: dict[str, Any]) -> Any:
    return _dispatch_sync_task(
        task_type,
        kwargs,
        task_runners=_SYNC_TASK_RUNNERS,
    )


def _is_interrupt_exception(value: Any) -> bool:
    return isinstance(
        value,
        (AsyncSqlCancelled, KeyboardInterrupt, asyncio.CancelledError),
    )


def _raise_interrupt_result(value: Any) -> None:
    if _is_interrupt_exception(value) and isinstance(value, BaseException):
        raise value

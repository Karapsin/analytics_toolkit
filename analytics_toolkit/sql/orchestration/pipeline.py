from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _PipelineContext:
    task_name: str
    step_index: int = 0
    results: list[Any] = field(default_factory=list)

    @property
    def last_result(self) -> Any:
        if not self.results:
            return None
        return self.results[-1]


def _is_async_callable(func: Any) -> bool:
    if inspect.iscoroutinefunction(func):
        return True
    call = getattr(func, "__call__", None)
    return inspect.iscoroutinefunction(call)

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TextIO

_TIME_PRINT_LEVELS = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
}
_TIME_PRINT_SINKS = {"print", "logging"}
_TIME_PRINT_LOGGER_NAME = "analytics_toolkit"
_time_print_level = "info"
_time_print_sink = "print"
_time_print_clock: Callable[[], datetime] = datetime.now


@dataclass(frozen=True)
class _TimePrintContext:
    operation: str | None = None
    connection: str | None = None
    backend: str | None = None
    phase: str | None = None
    task_id: str | None = None
    message_prefix: str | None = None


_time_print_context: ContextVar[_TimePrintContext] = ContextVar(
    "time_print_context",
    default=_TimePrintContext(),
)


def time_print(
    message: str,
    *,
    level: str = "info",
    enabled: bool = True,
    operation: str | None = None,
    connection: str | None = None,
    backend: str | None = None,
    phase: str | None = None,
    task_id: str | None = None,
    stream: str | TextIO | None = None,
) -> None:
    """Print a timestamped message with optional filtering and context."""

    normalized_level = _normalize_time_print_level(level)
    if not enabled or not _should_print_level(normalized_level):
        return

    current_time = _time_print_clock()
    if not isinstance(current_time, datetime):
        raise TypeError("clock must return a datetime.")
    formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
    context = _time_print_context.get()
    resolved_context = _TimePrintContext(
        operation=_normalize_context_part(operation) or context.operation,
        connection=_normalize_context_part(connection) or context.connection,
        backend=_normalize_context_part(backend) or context.backend,
        phase=_normalize_context_part(phase) or context.phase,
        task_id=_normalize_context_part(task_id) or context.task_id,
        message_prefix=context.message_prefix,
    )
    if resolved_context.message_prefix and not message.startswith(resolved_context.message_prefix):
        message = f"{resolved_context.message_prefix}{message}"
    formatted_message = _format_time_print_message(
        formatted_time,
        resolved_context,
        message,
    )
    if _time_print_sink == "logging" and stream is None:
        logging.getLogger(_TIME_PRINT_LOGGER_NAME).log(
            _TIME_PRINT_LEVELS[normalized_level],
            formatted_message,
        )
        return

    print(formatted_message, file=_resolve_stream(stream))


def set_time_print_level(level: str) -> None:
    """Set the minimum level printed by ``time_print``."""

    global _time_print_level
    _time_print_level = _normalize_time_print_level(level)


def get_time_print_level() -> str:
    """Return the current minimum ``time_print`` level."""

    return _time_print_level


def set_time_print_sink(sink: str) -> None:
    """Set where ``time_print`` sends messages: ``print`` or ``logging``."""

    global _time_print_sink
    _time_print_sink = _normalize_time_print_sink(sink)


def get_time_print_sink() -> str:
    """Return the current ``time_print`` sink."""

    return _time_print_sink


def set_time_print_clock(clock: Callable[[], datetime] | None) -> None:
    """Override the clock used by ``time_print`` or reset it with ``None``."""

    global _time_print_clock
    if clock is None:
        _time_print_clock = datetime.now
        return
    if not callable(clock):
        raise TypeError("clock must be callable or None.")
    _time_print_clock = clock


@contextmanager
def time_print_context(
    *,
    operation: str | None = None,
    connection: str | None = None,
    backend: str | None = None,
    phase: str | None = None,
    task_id: str | None = None,
) -> Iterator[None]:
    """Temporarily add structured context to ``time_print`` calls."""

    current = _time_print_context.get()
    token = _time_print_context.set(
        _TimePrintContext(
            operation=_normalize_context_part(operation) or current.operation,
            connection=_normalize_context_part(connection) or current.connection,
            backend=_normalize_context_part(backend) or current.backend,
            phase=_normalize_context_part(phase) or current.phase,
            task_id=_normalize_context_part(task_id) or current.task_id,
            message_prefix=current.message_prefix,
        )
    )
    try:
        yield
    finally:
        _time_print_context.reset(token)


@contextmanager
def _time_print_message_prefix(message_prefix: str) -> Iterator[None]:
    """Temporarily prefix messages without expanding the public context API."""
    current = _time_print_context.get()
    token = _time_print_context.set(
        _TimePrintContext(
            operation=current.operation,
            connection=current.connection,
            backend=current.backend,
            phase=current.phase,
            task_id=current.task_id,
            message_prefix=(_normalize_message_prefix(message_prefix) or current.message_prefix),
        )
    )
    try:
        yield
    finally:
        _time_print_context.reset(token)


def _normalize_time_print_level(level: str) -> str:
    if not isinstance(level, str):
        raise TypeError("level must be a string.")
    normalized = level.strip().lower()
    if normalized not in _TIME_PRINT_LEVELS:
        allowed = "', '".join(_TIME_PRINT_LEVELS)
        raise ValueError(f"level must be one of: '{allowed}'.")
    return normalized


def _normalize_time_print_sink(sink: str) -> str:
    if not isinstance(sink, str):
        raise TypeError("sink must be a string.")
    normalized = sink.strip().lower()
    if normalized not in _TIME_PRINT_SINKS:
        allowed = "', '".join(sorted(_TIME_PRINT_SINKS))
        raise ValueError(f"sink must be one of: '{allowed}'.")
    return normalized


def _should_print_level(level: str) -> bool:
    return _TIME_PRINT_LEVELS[level] >= _TIME_PRINT_LEVELS[_time_print_level]


def _normalize_context_part(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_message_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return f"{normalized} " if normalized else None


def _format_time_print_message(
    current_time: str,
    context: _TimePrintContext,
    message: str,
) -> str:
    prefix_parts = _format_context_parts(context)
    if not prefix_parts:
        return f"[{current_time}] {message}"
    return f"[{current_time}] {' '.join(prefix_parts)} {message}"


def _format_context_parts(context: _TimePrintContext) -> list[str]:
    parts: list[str] = []
    if context.operation is not None:
        parts.append(f"[{context.operation}]")
    if context.connection is not None and context.backend is not None:
        parts.append(f"[{context.connection}/{context.backend}]")
    elif context.connection is not None:
        parts.append(f"[{context.connection}]")
    elif context.backend is not None:
        parts.append(f"[{context.backend}]")
    if context.phase is not None:
        parts.append(f"[{context.phase}]")
    if context.task_id is not None:
        parts.append(f"[task_id={context.task_id}]")
    return parts


def _resolve_stream(stream: str | TextIO | None) -> TextIO:
    if stream is None or stream == "stdout":
        return sys.stdout
    if stream == "stderr":
        return sys.stderr
    if isinstance(stream, str):
        raise ValueError("stream must be 'stdout', 'stderr', a file-like object, or None.")
    if not hasattr(stream, "write"):
        raise TypeError("stream must expose a write method.")
    return stream

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any, TypeVar

from analytics_toolkit.sql.connection.get_sql_connection import get_sql_connection

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

T = TypeVar("T")


class BoundedConnectionCloseError(RuntimeError):
    """A pool connection could not be proven closed."""

    analytics_toolkit_sql_retry_safe = False


class BoundedConnectionManager:
    """Lazily open and reuse at most ``capacity`` connections for one alias."""

    def __init__(
        self,
        connection_key: str,
        capacity: int,
        *,
        role: str,
        open_connection: Callable[[str], Any] = get_sql_connection,
    ) -> None:
        if isinstance(capacity, bool) or type(capacity) is not int or capacity < 1:
            message = "connection capacity must be a positive integer."
            raise ValueError(message)
        self.connection_key = connection_key
        self.capacity = capacity
        self.role = role
        self._open_connection = open_connection
        self._refs: list[dict[str, Any]] = [{} for _ in range(capacity)]
        self._available: queue.LifoQueue[dict[str, Any]] = queue.LifoQueue(capacity)
        for ref in self._refs:
            self._available.put_nowait(ref)
        self._lock = threading.Lock()
        self._open_condition = threading.Condition(self._lock)
        self._close_lock = threading.Lock()
        self._closed = False
        self._interrupted = False
        self._leased: set[int] = set()
        self._open_count = 0
        self._high_water_mark = 0
        self._inflight_opens = 0

    @property
    def high_water_mark(self) -> int:
        with self._lock:
            return self._high_water_mark

    @contextmanager
    def lease(
        self,
        *,
        cancellation: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        ref = self._take_available(cancellation)
        try:
            with self._lock:
                self._leased.add(id(ref))
            self._ensure_open(ref)
            yield ref
        finally:
            with self._lock:
                self._leased.discard(id(ref))
            self._available.put_nowait(ref)

    def run(
        self,
        role: str,
        operation: Callable[[dict[str, Any]], T],
        *,
        cancellation: threading.Event | None = None,
    ) -> T:
        del role
        with self.lease(cancellation=cancellation) as ref:
            return operation(ref)

    def run_with_connection(
        self,
        role: str,
        open_connection: Callable[[], Any],
        operation: Callable[[Any], T],
        *,
        cancellation: threading.Event | None = None,
    ) -> T:
        """Run with a specialized connection while retaining the shared cap."""
        del role
        ref = self._take_available(cancellation)
        operation_error: BaseException | None = None
        try:
            with self._lock:
                self._leased.add(id(ref))
            self._close_ref_connection(ref, action="specialized connection replacement")
            self._begin_open("specialized connection")
            try:
                connection = open_connection()
                self._accept_or_reject_opened_connection(
                    connection,
                    ref,
                    action="specialized connection",
                )
            finally:
                self._finish_open()
            try:
                return operation(ref["connection"])
            except BaseException as exc:
                operation_error = exc
                raise
        finally:
            try:
                self._close_ref_connection(ref, action="specialized connection cleanup")
            except BaseException as close_error:
                if operation_error is None:
                    raise
                try:
                    operation_error.__dict__["analytics_toolkit_sql_retry_safe"] = False
                    add_note = getattr(operation_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "Bounded specialized target connection cleanup also failed: "
                            f"{type(close_error).__name__}"
                        )
                except (AttributeError, TypeError):
                    raise close_error from operation_error
            finally:
                with self._lock:
                    self._leased.discard(id(ref))
                self._available.put_nowait(ref)

    def close(self) -> None:
        with self._open_condition:
            self._closed = True
            while self._inflight_opens:
                self._open_condition.wait()
        failures: list[Exception] = []
        with self._close_lock:
            for ref in self._refs:
                connection = ref.get("connection")
                if connection is None:
                    continue
                try:
                    connection.close()
                except Exception as exc:  # noqa: BLE001 -- aggregate all driver close failures
                    failures.append(exc)
                    continue
                ref.pop("connection", None)
            with self._lock:
                self._open_count = sum(ref.get("connection") is not None for ref in self._refs)
        if failures:
            message = (
                f"Could not close {len(failures)} connection(s) in the bounded {self.role}; "
                "no additional connections will be opened."
            )
            raise BoundedConnectionCloseError(message) from failures[0]

    def close_preserving(self, error: BaseException | None) -> None:
        """Close strictly, preserving and de-retrying an earlier failure."""
        try:
            self.close()
        except BaseException as close_error:
            if error is None:
                raise
            try:
                error.__dict__["analytics_toolkit_sql_retry_safe"] = False
                add_note = getattr(error, "add_note", None)
                if callable(add_note):
                    add_note(
                        f"Bounded {self.role} cleanup also failed: {type(close_error).__name__}"
                    )
            except (AttributeError, TypeError):
                raise close_error from error

    def interrupt_active(self) -> None:
        """Prevent replacements and close currently leased connections."""
        with self._lock:
            self._interrupted = True
            active_refs = [ref for ref in self._refs if id(ref) in self._leased]
        with self._close_lock:
            for ref in active_refs:
                connection = ref.get("connection")
                if connection is None:
                    continue
                cancel = getattr(connection, "cancel", None)
                if callable(cancel):
                    with suppress(Exception):
                        cancel()
                closed = False
                with suppress(Exception):
                    connection.close()
                    closed = True
                if not closed:
                    # A failed close remains tracked so cleanup can prove whether
                    # the capacity slot is still live before opening a replacement.
                    continue
                ref.pop("connection", None)
            with self._lock:
                self._open_count = sum(ref.get("connection") is not None for ref in self._refs)

    def resume_for_cleanup(self) -> None:
        """Reopen the manager only after every interrupted lease has returned."""
        with self._lock:
            if self._leased:
                message = f"{self.role} still has active connection leases."
                raise RuntimeError(message)
            if self._closed:
                message = f"{self.role} connection manager is closed."
                raise RuntimeError(message)
            if not self._interrupted:
                return
        failures: list[Exception] = []
        with self._close_lock:
            for ref in self._refs:
                connection = ref.get("connection")
                if connection is None:
                    continue
                try:
                    connection.close()
                except Exception as exc:  # noqa: BLE001 -- aggregate all driver close failures
                    failures.append(exc)
                    continue
                ref.pop("connection", None)
        with self._lock:
            self._open_count = sum(ref.get("connection") is not None for ref in self._refs)
            if not failures:
                self._interrupted = False
        if failures:
            message = (
                f"Could not close {len(failures)} interrupted connection(s) in the bounded "
                f"{self.role}; cleanup will not open another connection."
            )
            raise BoundedConnectionCloseError(message) from failures[0]

    def replace_connection(
        self,
        connection_key: str,
        connection_ref: dict[str, Any],
    ) -> None:
        if connection_key != self.connection_key or not any(
            connection_ref is ref for ref in self._refs
        ):
            message = "Connection replacement does not belong to this manager."
            raise RuntimeError(message)
        with self._lock:
            if self._interrupted or self._closed:
                error = RuntimeError(
                    f"Connection replacement in the bounded {self.role} was cancelled."
                )
                error.analytics_toolkit_sql_retry_safe = False  # type: ignore[attr-defined]
                raise error
        self._close_ref_connection(connection_ref, action="connection replacement")
        self._begin_open("replacement")
        try:
            try:
                replacement = self._open_connection(self.connection_key)
            except Exception as exc:
                message = f"Could not replace a connection in the bounded {self.role}."
                raise RuntimeError(message) from exc
            self._accept_or_reject_opened_connection(
                replacement,
                connection_ref,
                action="replacement",
            )
        finally:
            self._finish_open()

    def ensure_connection(
        self,
        connection_key: str,
        connection_ref: dict[str, Any],
    ) -> None:
        """Reopen an empty managed ref for the next logical retry attempt."""
        if connection_key != self.connection_key or not any(
            connection_ref is ref for ref in self._refs
        ):
            message = "Connection reference does not belong to this manager."
            raise RuntimeError(message)
        self._ensure_open(connection_ref)

    def _take_available(
        self,
        cancellation: threading.Event | None,
    ) -> dict[str, Any]:
        while True:
            with self._lock:
                if self._closed:
                    message = f"{self.role} connection manager is closed."
                    raise RuntimeError(message)
                if self._interrupted:
                    message = f"{self.role} connection manager was interrupted."
                    raise RuntimeError(message)
            if cancellation is not None and cancellation.is_set():
                message = f"{self.role} connection lease cancelled."
                raise RuntimeError(message)
            try:
                return self._available.get(timeout=0.1)
            except queue.Empty:
                continue

    def _close_ref_connection(self, ref: dict[str, Any], *, action: str) -> None:
        with self._close_lock:
            connection = ref.get("connection")
            if connection is None:
                return
            try:
                connection.close()
            except BaseException as exc:
                error = BoundedConnectionCloseError(
                    f"Could not close a connection during {action} in the bounded "
                    f"{self.role}; no replacement was opened."
                )
                raise error from exc
            ref.pop("connection", None)
            with self._lock:
                self._open_count -= 1

    def _ensure_open(self, ref: dict[str, Any]) -> None:
        ref["bounded_replace_connection"] = self.replace_connection
        ref["bounded_ensure_connection"] = self.ensure_connection
        with self._lock:
            if self._interrupted or self._closed:
                message = f"{self.role} connection manager was interrupted."
                raise RuntimeError(message)
        if ref.get("connection") is not None:
            return
        self._begin_open("lease")
        try:
            connection = self._open_connection(self.connection_key)
            self._accept_or_reject_opened_connection(connection, ref, action="lease")
        finally:
            self._finish_open()

    def _begin_open(self, action: str) -> None:
        with self._open_condition:
            if self._interrupted or self._closed:
                raise self._opening_cancelled_error(action)
            self._inflight_opens += 1

    def _finish_open(self) -> None:
        with self._open_condition:
            self._inflight_opens -= 1
            self._open_condition.notify_all()

    def _accept_or_reject_opened_connection(
        self,
        connection: Any,
        ref: dict[str, Any],
        *,
        action: str,
    ) -> None:
        with self._lock:
            rejected = self._interrupted or self._closed
            if not rejected:
                ref["connection"] = connection
                self._open_count += 1
                self._high_water_mark = max(self._high_water_mark, self._open_count)
                return
        self._reject_opened_connection(connection, ref, action=action)
        raise self._opening_cancelled_error(action)

    def _opening_cancelled_error(self, action: str) -> RuntimeError:
        if action == "lease":
            message = f"{self.role} connection opening was cancelled."
        elif action == "replacement":
            message = f"Connection replacement in the bounded {self.role} was cancelled."
        else:
            message = f"{action.title()} in the bounded {self.role} was cancelled."
        error = RuntimeError(message)
        error.analytics_toolkit_sql_retry_safe = False  # type: ignore[attr-defined]
        return error

    def _reject_opened_connection(
        self,
        connection: Any,
        ref: dict[str, Any],
        *,
        action: str,
    ) -> None:
        try:
            connection.close()
        except BaseException as exc:
            with self._lock:
                if ref.get("connection") is None:
                    ref["connection"] = connection
                    self._open_count += 1
                    self._high_water_mark = max(self._high_water_mark, self._open_count)
            message = (
                f"Could not close a connection opened during cancelled {action} in the "
                f"bounded {self.role}; no additional connection will be opened."
            )
            raise BoundedConnectionCloseError(message) from exc

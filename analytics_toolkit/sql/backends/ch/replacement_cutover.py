"""Generation-bound permissions for acknowledged replacement name transitions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterator, Optional, Tuple

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.execution.backend_options import current_backend_operation

from .dialect import ClickHouseDialect
from .outage_statements import exchange_or_rename

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .drop_layout import VerifiedDropResource


_TableKey = Tuple[str, str, Optional[str]]
_CUTOVER_TABLE_COUNT = 2


def _cluster(connection: Any, value: str | None) -> str | None:
    if value is None:
        return None
    expression = parse_one(value, read=ClickHouseDialect)
    if not isinstance(expression, (exp.Identifier, exp.Column, exp.Literal)):
        message = "Replacement cutover requires one static execution cluster."
        raise InvalidSqlInputError(message)
    from .reconfigure_support import (  # noqa: PLC0415 - outage/resource metadata cycle.
        resolve_optional_cluster,
    )

    return resolve_optional_cluster(connection, expression.name)


def _table(table: exp.Expr, cluster: str | None) -> _TableKey:
    if (
        not isinstance(table, exp.Table)
        or not isinstance(table.this, exp.Identifier)
        or table.catalog
        or not table.db
    ):
        message = "Replacement cutover requires exact qualified table names."
        raise InvalidSqlInputError(message)
    return table.db, table.name, cluster


@dataclass(frozen=True)
class _Transition:
    operation: str
    tables: tuple[_TableKey, _TableKey]


def _transition(
    connection: Any, statement: exp.Expr, clusters: Sequence[str]
) -> _Transition | None:
    if (
        not isinstance(statement, exp.Command)
        or statement.this not in {"EXCHANGE", "RENAME"}
        or len(statement.expressions) != _CUTOVER_TABLE_COUNT
        or len(clusters) > 1
    ):
        return None
    cluster = _cluster(connection, clusters[0]) if clusters else None
    left, right = (_table(table, cluster) for table in statement.expressions)
    return _Transition(statement.this, (left, right))


def _parse_transition(connection: Any, sql: str) -> _Transition:
    special = exchange_or_rename(sql)
    transition = _transition(connection, *special) if special is not None else None
    if transition is None:
        message = "Replacement cutover accepts only one static EXCHANGE or RENAME."
        raise InvalidSqlInputError(message)
    return transition


def _resource_key(resource: VerifiedDropResource) -> _TableKey:
    return _table(
        parse_one(resource.table, read=ClickHouseDialect, into=exp.Table), resource.cluster
    )


def _retarget(resource: VerifiedDropResource, key: _TableKey) -> VerifiedDropResource:
    from .reconfigure_ddl import (  # noqa: PLC0415 - outage client/resource certification cycle.
        parse_create_table,
        retarget_create,
    )

    database, relation, _cluster_name = key
    create = parse_create_table(resource.ddl, resource.table)
    target = create.this.this.copy()
    target.set("this", exp.to_identifier(relation, quoted=bool(target.this.args.get("quoted"))))
    qualifier = target.args.get("db")
    target.set(
        "db", exp.to_identifier(database, quoted=bool(qualifier and qualifier.args.get("quoted")))
    )
    table = target.sql(dialect=ClickHouseDialect)
    create = retarget_create(create, table, None)
    return replace(resource, table=table, ddl=create.sql(dialect=ClickHouseDialect))


@dataclass
class CutoverStep:
    owner: CutoverResources
    transition: _Transition
    observed: bool = False
    submitted: bool = False


_ACTIVE_STEP: ContextVar[CutoverStep | None] = ContextVar("ch_replacement_cutover", default=None)


class CutoverResources:
    def __init__(
        self,
        connection: Any,
        originals: Sequence[VerifiedDropResource],
        owned: Sequence[tuple[str, str | None]],
        steps: Sequence[tuple[str, str]],
    ) -> None:
        from .drop_layout import (  # noqa: PLC0415 - outage client/resource certification cycle.
            ResourceInspection,
            inspect_resource,
        )

        self.connection = connection
        self.operation = current_backend_operation()
        self.allowed = {_parse_transition(connection, sql) for step in steps for sql in step}
        self.expected: dict[_TableKey, VerifiedDropResource | None] = {
            _resource_key(resource): resource for resource in originals
        }
        for table, cluster in owned:
            resource = inspect_resource(
                connection,
                table,
                cluster,
                "owned replacement",
                ResourceInspection(sqls=("SELECT 1",)),
            )
            if resource is None:
                message = "An acknowledged replacement resource is absent before cutover."
                raise InvalidSqlInputError(message)
            self.expected[_resource_key(resource)] = resource
        for transition in self.allowed:
            for key in transition.tables:
                if key not in self.expected:
                    self.expected[key] = None
                    self._require(key)

    def _require(self, key: _TableKey) -> None:
        from .drop_layout import (  # noqa: PLC0415 - outage client/resource certification cycle.
            ResourceInspection,
            inspect_resource,
            require_drop_resource,
        )

        resource = self.expected[key]
        if resource is not None:
            require_drop_resource(self.connection, resource, sqls=("SELECT 1",))
            return
        database, relation, cluster = key
        table = exp.Table(
            this=exp.to_identifier(relation, quoted=True),
            db=exp.to_identifier(database, quoted=True),
        ).sql(dialect=ClickHouseDialect)
        if (
            inspect_resource(
                self.connection,
                table,
                cluster,
                "vacant replacement destination",
                ResourceInspection(allow_absence=True, sqls=("SELECT 1",)),
            )
            is not None
        ):
            message = "The replacement cutover destination is no longer vacant."
            raise InvalidSqlInputError(message)

    def require(self) -> None:
        for key in self.expected:
            self._require(key)

    @contextmanager
    def step(self, sql: str) -> Iterator[CutoverStep]:
        transition = _parse_transition(self.connection, sql)
        if transition not in self.allowed:
            message = "The statement is outside the certified replacement cutover."
            raise InvalidSqlInputError(message)
        step = CutoverStep(self, transition)
        token = _ACTIVE_STEP.set(step)
        try:
            self.require()
            yield step
        finally:
            _ACTIVE_STEP.reset(token)

    def acknowledge(self, sql: str) -> None:
        transition = _parse_transition(self.connection, sql)
        left, right = transition.tables
        source, destination = self.expected[left], self.expected[right]
        if source is None or (transition.operation == "EXCHANGE" and destination is None):
            message = "The acknowledged cutover has no complete generation transition."
            raise InvalidSqlInputError(message)
        self.expected[right] = _retarget(source, right)
        self.expected[left] = _retarget(destination, left) if destination is not None else None


def _active(connection: Any, statement: exp.Expr, clusters: Sequence[str]) -> CutoverStep | None:
    step = _ACTIVE_STEP.get()
    if (
        step is None
        or step.owner.connection is not connection
        or step.owner.operation is not current_backend_operation()
        or _transition(connection, statement, clusters) != step.transition
    ):
        return None
    return step


def preflight_cutover(connection: Any, statement: exp.Expr, clusters: Sequence[str]) -> bool:
    step = _active(connection, statement, clusters)
    if step is None:
        return False
    step.observed = True
    step.owner.require()
    return True


def mark_cutover_submitted(connection: Any, statement: exp.Expr, clusters: Sequence[str]) -> None:
    step = _active(connection, statement, clusters)
    if step is not None:
        step.submitted = True

"""Owned native assignments certify stored values without weakening type domains."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from ipaddress import ip_network
from typing import TYPE_CHECKING, Any

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentError,
    certify_assignment_checkpoint,
    native_assignment_errors,
    requires_native_assignment,
)
from analytics_toolkit.sql.core.replay_spool import FrozenInputIdentity
from analytics_toolkit.sql.dml.transfer.runtime.retry import is_non_retryable_sql_error
from analytics_toolkit.sql.execution.cancellation import (
    AsyncSqlCancelled,
    SqlCancellationScope,
    activate_cancellation_scope,
)
from psycopg2.extensions import adapt
from sqlglot import exp, parse_one

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_STAGE = '"scratch%schema"."assignment%stage"'
_DOMAIN = '"private%schema"."limited%value"'
_SECRET = "private-customer-input"
_BAD_INPUT = "22P02"
_TOO_LONG = "22001"


class _DatabaseError(RuntimeError):
    def __init__(self, code: Any = None) -> None:
        super().__init__(_SECRET)
        self.pgcode = code


class _ProbeCursor:
    def __init__(self, connection: _ProbeConnection) -> None:
        self.connection = connection
        self.closed = False

    def execute(self, query: str) -> None:
        self.connection.queries.append(query)
        if self.connection.on_execute is not None:
            self.connection.on_execute(query)
        if self.connection.failure is not None:
            raise self.connection.failure

    def fetchone(self) -> Any:
        return next(self.connection.results)

    def close(self) -> None:
        self.closed = True
        if self.connection.close_failure:
            raise RuntimeError(_SECRET)


class _ProbeConnection:
    def __init__(
        self,
        rows: Sequence[Any] = (),
        *,
        failure: Exception | None = None,
        close_failure: bool = False,
        on_execute: Callable[[str], None] | None = None,
    ) -> None:
        self.results = iter(rows)
        self.failure = failure
        self.close_failure = close_failure
        self.on_execute = on_execute
        self.queries: list[str] = []
        self.cursors: list[_ProbeCursor] = []

    def cursor(self) -> _ProbeCursor:
        cursor = _ProbeCursor(self)
        self.cursors.append(cursor)
        return cursor


class _AssignmentCursor(_ProbeCursor):
    def __init__(self, connection: _AssignmentConnection) -> None:
        super().__init__(connection)
        self.connection = connection
        self.result: Any = None

    def mogrify(self, template: str | bytes, values: Sequence[Any]) -> bytes:
        if isinstance(template, str):
            template = template.encode("utf-8")
        return template % tuple(adapt(value).getquoted() for value in values)

    def execute(self, query: str | bytes) -> None:
        text = query.decode("utf-8") if isinstance(query, bytes) else query
        self.connection.queries.append(text)
        statement = parse_one(text, read="postgres")
        if isinstance(statement, exp.Insert):
            assert isinstance(statement.this, exp.Schema)
            assert isinstance(statement.expression, exp.Values)
            names = [name.name for name in statement.this.expressions]
            assigned = [
                {
                    name: self.connection.assign(name, _literal(value))
                    for name, value in zip(names, row.expressions)
                }
                for row in statement.expression.expressions
            ]
            self.connection.rows.extend(assigned)
        elif statement.find(exp.Group) is not None:
            keys = [key.name for key in statement.find(exp.Group).expressions]
            values = [tuple(row[key] for key in keys) for row in self.connection.rows]
            self.result = (1,) if len(set(values)) != len(values) else None
        elif statement.find(exp.Count) is not None:
            assert statement.find(exp.Cast) is None
            selected = statement.find(exp.Subquery).this
            names = [item.name for item in selected.expressions]
            self.result = (
                len(self.connection.rows),
                *(sum(row[name] is None for row in self.connection.rows) for name in names),
            )
        else:
            assert isinstance(statement, exp.Select)
            self.result = (_literal(statement.expressions[0]),)

    def fetchone(self) -> Any:
        return self.result


def _literal(value: exp.Expression) -> Any:
    if isinstance(value, exp.Null):
        return None
    if isinstance(value, exp.Literal):
        return value.this if value.is_string else int(value.this)
    if isinstance(value, exp.Array):
        return [_literal(item) for item in value.expressions]
    assert isinstance(value, exp.Cast)
    item = _literal(value.this)
    native = value.args["to"].sql(dialect="postgres")
    if native == _DOMAIN:
        return item[:3] if item is not None else None
    return item


class _AssignmentConnection(_ProbeConnection):
    encoding = "UTF8"

    def __init__(self, column_types: Mapping[str, str]) -> None:
        super().__init__()
        self.column_types = dict(column_types)
        self.rows: list[dict[str, Any]] = []
        self.committed_rows: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _AssignmentCursor:
        cursor = _AssignmentCursor(self)
        self.cursors.append(cursor)
        return cursor

    def assign(self, name: str, value: Any) -> Any:
        if value is None:
            return None
        native = self.column_types[name]
        if native == "CIDR":
            try:
                return str(ip_network(value, strict=True))
            except ValueError:
                raise _DatabaseError(_BAD_INPUT) from None
        if native == "MACADDR":
            if len(value.split(":")) != 6 or any(len(part) != 2 for part in value.split(":")):
                raise _DatabaseError(_BAD_INPUT)
            return value.lower()
        if native == _DOMAIN:
            if len(value) > 3:
                raise _DatabaseError(_TOO_LONG)
            return value
        assert native == _DOMAIN + "[]"
        if any(item is not None and len(item) > 3 for item in value):
            raise _DatabaseError(_TOO_LONG)
        return value

    def commit(self) -> None:
        self.committed_rows = [dict(row) for row in self.rows]
        self.commits += 1

    def rollback(self) -> None:
        self.rows = [dict(row) for row in self.committed_rows]
        self.rollbacks += 1


def _populate(connection: _AssignmentConnection, rows: Sequence[Sequence[Any]]) -> None:
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        _STAGE,
        list(connection.column_types),
        rows,
        target_column_types=connection.column_types,
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label="native_assignment",
        on_progress=None,
    )


@pytest.mark.parametrize(
    "native",
    ["CIDR", "MACADDR", _DOMAIN, _DOMAIN + "[]", '"other"."custom_type"'],
)
def test_unmodeled_native_assignment_is_generic_and_preserves_type_identity(native):
    assert requires_native_assignment(native)


@pytest.mark.parametrize("native", ["BIGINT", "INET", "JSONB[]", '"pg_catalog"."numeric"(8,2)'])
def test_locally_certifiable_builtins_keep_the_existing_conversion_path(native):
    assert not requires_native_assignment(native)


@pytest.mark.parametrize(
    ("native", "value"),
    [("CIDR", "192.0.2.0/24"), ("MACADDR", "08:00:2b:01:02:03"), (_DOMAIN, "abc")],
)
def test_real_execute_values_assigns_native_parameters_without_text_fallback(native, value):
    adapter = get_backend_adapter("gp")
    connection = _AssignmentConnection({"payload": native})
    with native_assignment_errors("population"):
        _populate(connection, [[value], [None]])
    certificate = certify_assignment_checkpoint(
        adapter, connection, _STAGE, connection.column_types, expected_rows=2
    )
    assert connection.rows == [{"payload": value}, {"payload": None}]
    assert certificate.column_types == (("payload", native),)
    assert certificate.null_columns == ("payload",)
    assert certificate.row_count == 2
    assert connection.commits == 1
    assert connection.rollbacks == 0
    insertion = parse_one(connection.queries[0], read="postgres")
    assert isinstance(insertion.expression, exp.Values)
    assert insertion.find(exp.Select) is None
    assert insertion.find(exp.Cast) is None
    assert all(cursor.closed for cursor in connection.cursors)


def test_real_execute_values_retains_quoted_array_type_and_literal_percent_context():
    # Arrays of domains here model PostgreSQL 11+ / Greenplum 7, not PG9.4 / GP6.
    native = _DOMAIN + "[]"
    connection = _AssignmentConnection({"payload%": native})
    _populate(connection, [[["a%", None, "bc"]]])
    certificate = certify_assignment_checkpoint(
        get_backend_adapter("gp"), connection, _STAGE, connection.column_types, expected_rows=1
    )
    assert connection.rows == [{"payload%": ["a%", None, "bc"]}]
    assert certificate.column_types == (("payload%", native),)
    insertion = parse_one(connection.queries[0], read="postgres")
    assert insertion.this.this.db == "scratch%schema"
    assert insertion.this.this.name == "assignment%stage"
    assert insertion.this.expressions[0].name == "payload%"
    assert insertion.find(exp.Cast).args["to"].sql(dialect="postgres") == native
    assert "%%" not in connection.queries[0]


def test_domain_assignment_rejects_values_that_an_explicit_cast_can_truncate():
    connection = _AssignmentConnection({"payload": _DOMAIN})
    cursor = connection.cursor()
    cursor.execute(f"SELECT CAST('four' AS {_DOMAIN})")
    assert cursor.fetchone() == ("fou",)
    cursor.close()
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors("population"):
        _populate(connection, [["four"]])
    assert caught.value.sqlstate == "22001"
    assert is_non_retryable_sql_error(caught.value)
    assert _SECRET not in str(caught.value)
    assert connection.rows == []
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert all(cursor.closed for cursor in connection.cursors)


def test_certificate_binds_all_stored_columns_exact_schema_order_and_input_generation():
    schema = {"Id": "CIDR", " nullable ": _DOMAIN}
    identity = FrozenInputIdentity(
        source_key="native_alias", configuration_digest="frozen", namespace=("scratch%schema",)
    )
    connection = _ProbeConnection([(3, 0, 1), None])
    with activate_cancellation_scope(SqlCancellationScope()):
        certificate = certify_assignment_checkpoint(
            get_backend_adapter("gp"),
            connection,
            _STAGE,
            schema,
            expected_rows=3,
            key_columns=["Id", "Id"],
            input_identity=identity,
            query_label="certified_native_input",
        )
    assert certificate.stage_table == _STAGE
    assert certificate.column_types == tuple(schema.items())
    assert certificate.row_count == 3
    assert certificate.key_columns == ("Id",)
    assert certificate.null_columns == (" nullable ",)
    assert certificate.input_identity is identity
    schema["Id"] = "TEXT"
    assert certificate.column_types[0] == ("Id", "CIDR")
    with pytest.raises(FrozenInstanceError):
        certificate.row_count = 4
    assert len(connection.queries) == 2
    for query in connection.queries:
        assert "query_label=certified_native_input" in query
        assert "async_sql=" in query
        assert parse_one(query, read="postgres").find(exp.Cast) is None
    counts = parse_one(connection.queries[0], read="postgres")
    assert counts.find(exp.Table).db == "scratch%schema"
    assert counts.find(exp.Table).name == "assignment%stage"
    assert connection.queries[1].endswith('GROUP BY "Id" HAVING COUNT(*) > 1 LIMIT 1')
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("expected", [None, True, -1, 1.0, "1"])
def test_certificate_requires_an_exact_complete_input_count_without_a_probe(expected):
    connection = _ProbeConnection()
    with pytest.raises(NativeAssignmentError, match="exact input count"):
        certify_assignment_checkpoint(
            get_backend_adapter("gp"),
            connection,
            _STAGE,
            {"payload": "CIDR"},
            expected_rows=expected,
        )
    assert connection.queries == []


@pytest.mark.parametrize("actual", [0, 2])
def test_certificate_rejects_incomplete_or_extra_stored_rows(actual):
    connection = _ProbeConnection([(actual, 0)])
    with pytest.raises(NativeAssignmentError, match="row count"):
        certify_assignment_checkpoint(
            get_backend_adapter("gp"), connection, _STAGE, {"payload": "CIDR"}, expected_rows=1
        )
    assert len(connection.queries) == 1
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize(
    "row", [None, (), (1,), (-1, 0), (1, -1), (1, 2), (True, 0), (1, False), (1, 0, 0), ("1", 0)]
)
def test_certificate_rejects_malformed_actual_stage_counts(row):
    connection = _ProbeConnection([row])
    with pytest.raises(NativeAssignmentError) as caught:
        certify_assignment_checkpoint(
            get_backend_adapter("gp"), connection, _STAGE, {"payload": "CIDR"}, expected_rows=1
        )
    assert is_non_retryable_sql_error(caught.value)
    assert all(cursor.closed for cursor in connection.cursors)


def test_certificate_rejects_missing_or_null_native_key_columns_before_duplicate_scan():
    missing = _ProbeConnection()
    with pytest.raises(NativeAssignmentError, match="missing"):
        certify_assignment_checkpoint(
            get_backend_adapter("gp"),
            missing,
            _STAGE,
            {"payload": "CIDR"},
            expected_rows=1,
            key_columns=["absent"],
        )
    assert missing.queries == []
    nulls = _ProbeConnection([(1, 1)])
    with pytest.raises(NativeAssignmentError, match="SQL NULL"):
        certify_assignment_checkpoint(
            get_backend_adapter("gp"),
            nulls,
            _STAGE,
            {"payload": "CIDR"},
            expected_rows=1,
            key_columns=["payload"],
        )
    assert len(nulls.queries) == 1


def test_native_key_duplicates_are_checked_after_real_native_assignment():
    connection = _AssignmentConnection({"network": "CIDR"})
    _populate(connection, [["2001:db8::/64"], ["2001:0db8::/64"]])
    assert connection.rows == [{"network": "2001:db8::/64"}] * 2
    with pytest.raises(NativeAssignmentError, match="Duplicate"):
        certify_assignment_checkpoint(
            get_backend_adapter("gp"),
            connection,
            _STAGE,
            connection.column_types,
            expected_rows=2,
            key_columns=["network"],
        )
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("phase", ["creation", "population", "validation"])
@pytest.mark.parametrize(
    "code", ["22P02", "22001", "23514", "23505", "42601", "3F000", "0A000", "42501"]
)
def test_native_failures_hide_values_and_keep_deterministic_resource_evidence(phase, code):
    failure = _DatabaseError(code)
    failure.analytics_toolkit_sql_stage_candidate = _STAGE
    resources = ((_STAGE, "physical"),)
    failure.analytics_toolkit_sql_acknowledged_resources = resources
    failure.__notes__ = [_SECRET]
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors(phase):
        raise failure
    error = caught.value
    assert error.sqlstate == code
    assert phase in str(error)
    assert _SECRET not in str(error)
    assert _SECRET not in " ".join(getattr(error, "__notes__", ()))
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert error.analytics_toolkit_sql_stage_candidate == _STAGE
    assert error.analytics_toolkit_sql_acknowledged_resources is resources
    assert is_non_retryable_sql_error(error)
    if code == "42501":
        assert "CREATE" in str(error)
        assert "USAGE" in str(error)


@pytest.mark.parametrize("code", ["08006", "40001", "53100", "55P03"])
def test_native_transient_failures_keep_automatic_replay_eligible(code):
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors("population"):
        raise _DatabaseError(code)
    assert caught.value.analytics_toolkit_sql_retry_safe
    assert not is_non_retryable_sql_error(caught.value)


@pytest.mark.parametrize("kind", [TypeError, ValueError])
def test_adapter_value_and_configuration_errors_are_safe_and_nonretryable(kind):
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors("creation"):
        raise kind(_SECRET)
    assert caught.value.sqlstate is None
    assert _SECRET not in str(caught.value)
    assert is_non_retryable_sql_error(caught.value)


@pytest.mark.parametrize("code", ["22P02-private", "22P\n2", 22001, "23🐍00"])
def test_invalid_sqlstate_attributes_cannot_become_payload_diagnostics(code):
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors("population"):
        raise _DatabaseError(code)
    assert caught.value.sqlstate is None
    assert _SECRET not in str(caught.value)
    assert "SQLSTATE" not in str(caught.value)


def test_caller_retry_classification_and_existing_safe_errors_are_preserved():
    failure = _DatabaseError("08006")
    originals = []
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors(
        "creation", non_retryable_predicate=lambda error: originals.append(error) or True
    ):
        raise failure
    assert originals == [failure]
    assert is_non_retryable_sql_error(caught.value)
    failure.analytics_toolkit_sql_retry_safe = False
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors("population"):
        raise failure
    assert is_non_retryable_sql_error(caught.value)
    with pytest.raises(NativeAssignmentError) as repeated, native_assignment_errors("validation"):
        raise caught.value
    assert repeated.value is caught.value


@pytest.mark.parametrize("when", ["before", "counts", "duplicates"])
def test_cancellation_stops_native_certification_without_wrapping_or_more_queries(when):
    scope = SqlCancellationScope()
    if when == "before":
        scope.request_cancel()

    def cancel(query):
        if (when == "counts" and "COUNT(*) - COUNT" in query) or (
            when == "duplicates" and "GROUP BY" in query
        ):
            scope.request_cancel()

    connection = _ProbeConnection([(1, 0), None], on_execute=cancel)
    with activate_cancellation_scope(scope), pytest.raises(AsyncSqlCancelled):
        certify_assignment_checkpoint(
            get_backend_adapter("gp"),
            connection,
            _STAGE,
            {"payload": "CIDR"},
            expected_rows=1,
            key_columns=["payload"],
        )
    assert len(connection.queries) == {"before": 0, "counts": 1, "duplicates": 2}[when]
    assert all(cursor.closed for cursor in connection.cursors)


def test_cancellation_baseexception_is_not_sanitized_as_a_retryable_sql_failure():
    cancellation = AsyncSqlCancelled("cancel native assignment")
    with pytest.raises(AsyncSqlCancelled) as caught, native_assignment_errors("population"):
        raise cancellation
    assert caught.value is cancellation


def test_cursor_close_failure_preserves_success_and_the_primary_native_failure(capsys):
    connection = _ProbeConnection([(1, 0), None], close_failure=True)
    certificate = certify_assignment_checkpoint(
        get_backend_adapter("gp"),
        connection,
        _STAGE,
        {"payload": "CIDR"},
        expected_rows=1,
        key_columns=["payload"],
    )
    assert certificate.row_count == 1
    assert len(connection.queries) == 2
    failure = _ProbeConnection(failure=_DatabaseError("22P02"), close_failure=True)
    with pytest.raises(NativeAssignmentError) as caught:
        certify_assignment_checkpoint(
            get_backend_adapter("gp"), failure, _STAGE, {"payload": "CIDR"}, expected_rows=1
        )
    assert caught.value.sqlstate == "22P02"
    assert is_non_retryable_sql_error(caught.value)
    output = capsys.readouterr().out
    assert "Cursor close failed" in output
    assert _SECRET not in output
    assert all(cursor.closed for cursor in [*connection.cursors, *failure.cursors])

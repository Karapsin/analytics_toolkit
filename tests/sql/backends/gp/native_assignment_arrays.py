"""Unbounded native array binds leave domain constraints to typed assignment.

Domain-array fixtures model PostgreSQL 11+ servers, including Greenplum 7.
PostgreSQL 9.4 / Greenplum 6 support scalar domains, not arrays of domains.
"""

from __future__ import annotations

from ipaddress import ip_network
from typing import TYPE_CHECKING, Any

import pytest
from analytics_toolkit.sql.backends import get_backend_adapter
from analytics_toolkit.sql.backends.gp.native_assignment import (
    NativeAssignmentError,
    assignment_parameter_types,
    certify_assignment_checkpoint,
    native_assignment_errors,
)
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

_STAGE = '" scratch.with.dot% "." native%stage "'
_DOMAIN = '" private%schema "."limited%value"'
_BASE = '"pg_catalog"."varchar"'
_ARRAY_OID = 9002
_DOMAIN_OID = 9001
_VARCHAR_OID = 1043
_SECRET = "private-array-input"


class _DatabaseError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(_SECRET)
        self.pgcode = code


def _domain_types() -> dict[int, Any]:
    return {
        _ARRAY_OID: (0, _DOMAIN_OID, _DOMAIN + "[]", '"private%schema"."_limited%value"'),
        _DOMAIN_OID: (_VARCHAR_OID, 0, _DOMAIN, _DOMAIN),
        _VARCHAR_OID: (0, 0, "character varying", _BASE),
    }


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.closed = False
        self.result: Any = None

    def mogrify(self, template: str | bytes, values: Sequence[Any]) -> bytes:
        if isinstance(template, str):
            template = template.encode("utf-8")
        return template % tuple(adapt(value).getquoted() for value in values)

    def execute(self, query: str | bytes, params: tuple[Any, ...] | None = None) -> None:
        text = query.decode("utf-8") if isinstance(query, bytes) else query
        if params is not None:
            self.connection.catalog.append((text, params))
            if self.connection.on_catalog is not None:
                self.connection.on_catalog(params)
            if self.connection.failure is not None:
                raise self.connection.failure
            self.result = (
                self.connection.attributes
                if len(params) == 3
                else self.connection.types.get(params[0])
            )
            return
        self.connection.statements.append(text)
        tree = parse_one(text, read="postgres")
        if isinstance(tree, exp.Insert):
            assert isinstance(tree.this, exp.Schema)
            assert isinstance(tree.expression, exp.Values)
            names = [name.name for name in tree.this.expressions]
            assigned = [
                {
                    name: self.connection.assign(name, _value(node, array_context=True))
                    for name, node in zip(names, row.expressions)
                }
                for row in tree.expression.expressions
            ]
            self.connection.rows.extend(assigned)
        else:
            assert tree.find(exp.Cast) is None
            selected = tree.find(exp.Subquery).this
            self.result = (
                len(self.connection.rows),
                *(
                    sum(row[column.name] is None for row in self.connection.rows)
                    for column in selected.expressions
                ),
            )

    def fetchall(self) -> Any:
        return self.result

    def fetchone(self) -> Any:
        return self.result

    def close(self) -> None:
        self.closed = True
        if self.connection.close_failure:
            raise RuntimeError(_SECRET)


def _value(node: exp.Expression, *, array_context: bool = False, truncate: bool = False) -> Any:
    while isinstance(node, exp.Cast):
        native = node.args["to"].sql(dialect="postgres")
        truncate |= native.rstrip("[]") in {_DOMAIN, "VARCHAR(3)"}
        node = node.this
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Array):
        return [_value(item, truncate=truncate) for item in node.expressions]
    assert isinstance(node, exp.Literal)
    assert node.is_string
    if array_context and node.this.startswith("{"):
        assert node.this == "{}" or set(node.this[1:-1].split(",")) == {"NULL"}
        return [] if node.this == "{}" else [None] * len(node.this[1:-1].split(","))
    return node.this[:3] if truncate else node.this


def _map_leaves(value: Any, function: Callable[[Any], Any]) -> Any:
    return (
        [_map_leaves(item, function) for item in value]
        if isinstance(value, list)
        else function(value)
    )


class _Connection:
    encoding = "UTF8"

    def __init__(
        self,
        schema: Mapping[str, str] | None = None,
        *,
        attributes: Any = None,
        types: Mapping[int, Any] | None = None,
        failure: Exception | None = None,
        close_failure: bool = False,
    ) -> None:
        self.schema = dict(schema or {"payload": _DOMAIN + "[]"})
        self.attributes = (
            [(name, _ARRAY_OID, 1) for name in self.schema] if attributes is None else attributes
        )
        self.types = _domain_types() if types is None else dict(types)
        self.failure = failure
        self.close_failure = close_failure
        self.on_catalog: Callable[[tuple[Any, ...]], None] | None = None
        self.catalog: list[tuple[str, tuple[Any, ...]]] = []
        self.statements: list[str] = []
        self.cursors: list[_Cursor] = []
        self.rows: list[dict[str, Any]] = []
        self.committed: list[dict[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        cursor = _Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def assign(self, name: str, value: Any) -> Any:
        def leaf(item: Any) -> Any:
            if item is None:
                return None
            if self.schema[name].upper() == "CIDR[]":
                return str(ip_network(item, strict=True))
            if len(item) > 3:
                code = "22001"
                raise _DatabaseError(code)
            return item

        return _map_leaves(value, leaf)

    def commit(self) -> None:
        self.committed = [dict(row) for row in self.rows]
        self.commits += 1

    def rollback(self) -> None:
        self.rows = [dict(row) for row in self.committed]
        self.rollbacks += 1


def _bind(connection: _Connection, *, label: str | None = None) -> dict[str, str]:
    return assignment_parameter_types(
        get_backend_adapter("gp"), connection, _STAGE, connection.schema, query_label=label
    )


def _populate(connection: _Connection, rows: Sequence[Sequence[Any]], parameter_types) -> None:
    get_backend_adapter("gp").insert_rows_batch(
        connection,
        _STAGE,
        list(connection.schema),
        rows,
        target_column_types=parameter_types,
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=10,
        connection_type="gp",
        query_label=None,
        on_progress=None,
    )


def test_catalog_parameter_type_prevents_explicit_domain_array_cast_truncation():
    original = _Connection()
    _populate(original, [[["long"]]], original.schema)
    assert original.rows == [{"payload": ["lon"]}]
    safe = _Connection()
    parameter_types = _bind(safe)
    assert parameter_types == {"payload": _BASE + "[]"}
    with pytest.raises(NativeAssignmentError) as caught, native_assignment_errors("population"):
        _populate(safe, [[["long"]]], parameter_types)
    assert caught.value.sqlstate == "22001"
    assert is_non_retryable_sql_error(caught.value)
    assert safe.rows == []
    assert safe.rollbacks == 1
    assert safe.commits == 0
    assert _SECRET not in str(caught.value)
    assert all(cursor.closed for cursor in [*original.cursors, *safe.cursors])


@pytest.mark.parametrize("value", [["ok", None, "a%"], [], [None], None, [["a"], ["bc"]]])
def test_native_domain_array_values_keep_null_empty_and_nested_payloads(value):
    connection = _Connection()
    parameter_types = _bind(connection)
    with native_assignment_errors("population"):
        _populate(connection, [[value]], parameter_types)
    certificate = certify_assignment_checkpoint(
        get_backend_adapter("gp"), connection, _STAGE, connection.schema, expected_rows=1
    )
    assert connection.rows == [{"payload": value}]
    assert certificate.column_types == (("payload", _DOMAIN + "[]"),)
    assert certificate.row_count == 1
    assert certificate.null_columns == (("payload",) if value is None else ())
    assert connection.schema == {"payload": _DOMAIN + "[]"}
    assert connection.commits == 1
    assert all(cursor.closed for cursor in connection.cursors)


def test_cidr_arrays_bind_the_catalog_leaf_type_without_a_family_whitelist():
    types = {
        651: (0, 650, "cidr[]", '"pg_catalog"."_cidr"'),
        650: (0, 0, "cidr", '"pg_catalog"."cidr"'),
    }
    connection = _Connection({"payload": "CIDR[]"}, attributes=[("payload", 651, 1)], types=types)
    parameter_types = _bind(connection)
    assert parameter_types == {"payload": '"pg_catalog"."cidr"[]'}
    _populate(connection, [[["192.0.2.0/24", None]]], parameter_types)
    assert connection.rows == [{"payload": ["192.0.2.0/24", None]}]
    assert connection.schema == {"payload": "CIDR[]"}


def test_server_quoted_leaf_names_and_percent_values_reach_real_execute_values_unchanged():
    qualified = '"base% name"."CaseSensitive%leaf"'
    types = {9002: (0, 9001, "custom[]", "custom_array"), 9001: (0, 0, "custom", qualified)}
    schema = {'native%"value': '"private% ns"."opaque%domain"[]'}
    connection = _Connection(schema, attributes=[('native%"value', 9002, 1)], types=types)
    parameter_types = _bind(connection)
    assert parameter_types == {'native%"value': qualified + "[]"}
    _populate(connection, [[["a%", "bc"]]], parameter_types)
    assert connection.rows == [{'native%"value': ["a%", "bc"]}]
    insertion = parse_one(connection.statements[0], read="postgres")
    assert insertion.this.this.db == " scratch.with.dot% "
    assert insertion.this.this.name == " native%stage "
    assert insertion.this.expressions[0].name == 'native%"value'
    assert insertion.find(exp.Cast).args["to"].sql(dialect="postgres") == qualified + "[]"
    assert "%%" not in connection.statements[0]


def test_catalog_bindings_preserve_namespace_columns_raw_types_and_cached_oid_identity():
    schema = {
        "known": "INTEGER[]",
        " first% ": _DOMAIN + "[]",
        "scalar": _DOMAIN,
        "second": _DOMAIN + "[][]",
    }
    connection = _Connection(
        schema, attributes=[("second", _ARRAY_OID, 2), (" first% ", _ARRAY_OID, 1)]
    )
    scope = SqlCancellationScope()
    with activate_cancellation_scope(scope):
        result = _bind(connection, label="native_array_catalog")
    assert result == {**schema, " first% ": _BASE + "[]", "second": _BASE + "[][]"}
    assert connection.schema == schema
    assert result is not connection.schema
    query, params = connection.catalog[0]
    assert params == (" scratch.with.dot% ", " native%stage ", [" first% ", "second"])
    assert " scratch.with.dot% " not in query
    assert " native%stage " not in query
    assert [params for _query, params in connection.catalog[1:]] == [
        (_ARRAY_OID,),
        (_DOMAIN_OID,),
        (_VARCHAR_OID,),
    ]
    for statement, _params in connection.catalog:
        assert "query_label=native_array_catalog" in statement
        assert scope.marker in statement
    type_query = connection.catalog[1][0]
    tree = parse_one(type_query, read="postgres")
    case = tree.find(exp.Case)
    assert case.args["ifs"][0].this.left.name == "typinput"
    assert case.args["ifs"][0].this.right.find(exp.Literal).this == "pg_catalog.array_in"
    assert case.args["ifs"][0].args["true"].name == "typelem"
    assert case.args["default"].this == "0"
    functions = {function.name.upper(): function for function in tree.find_all(exp.Anonymous)}
    assert functions["FORMAT_TYPE"].expressions[1].sql() == "-1"
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("dimensions", [0, 1, 3])
def test_bind_rank_preserves_declared_dimensions_and_actual_array_edges(dimensions):
    connection = _Connection(attributes=[("payload", _ARRAY_OID, dimensions)])
    assert _bind(connection) == {"payload": _BASE + "[]" * max(dimensions, 1)}


def test_nested_domains_are_unwrapped_before_using_the_unbounded_scalar_leaf():
    types = _domain_types()
    types[_DOMAIN_OID] = (9003, 0, "second_domain", '"private"."second_domain"')
    types[9003] = (_VARCHAR_OID, 0, "last_domain", '"private"."last_domain"')
    connection = _Connection(types=types)
    assert _bind(connection) == {"payload": _BASE + "[]"}
    assert [params[0] for _query, params in connection.catalog[1:]] == [
        _ARRAY_OID,
        _DOMAIN_OID,
        9003,
        _VARCHAR_OID,
    ]


def test_nonarray_typelem_does_not_get_unwrapped_as_an_array_leaf():
    types = {
        9002: (0, 600, "point[]", '"pg_catalog"."_point"'),
        600: (0, 0, "point", '"pg_catalog"."point"'),
    }
    connection = _Connection(types=types)
    assert _bind(connection) == {"payload": '"pg_catalog"."point"[]'}
    assert [params[0] for _query, params in connection.catalog[1:]] == [9002, 600]


@pytest.mark.parametrize(
    "schema",
    [{}, {"known": "INTEGER[]"}, {"scalar": _DOMAIN}, {"known": '"pg_catalog"."numeric"(8,2)[]'}],
)
def test_known_arrays_and_nonarray_columns_need_no_catalog_connection(schema):
    result = assignment_parameter_types(get_backend_adapter("gp"), object(), _STAGE, schema)
    assert result == schema
    assert result is not schema


@pytest.mark.parametrize(
    "attributes",
    [
        [],
        [("wrong", _ARRAY_OID, 1)],
        [("payload",)],
        [("payload", 0, 1)],
        [("payload", True, 1)],
        [("payload", _ARRAY_OID, -1)],
        [("payload", _ARRAY_OID, True)],
        [("payload", _ARRAY_OID, 1), ("payload", _ARRAY_OID, 1)],
    ],
)
def test_missing_duplicate_or_invalid_stage_attribute_metadata_fails_closed(attributes):
    connection = _Connection(attributes=attributes)
    with pytest.raises(NativeAssignmentError) as caught:
        _bind(connection)
    assert is_non_retryable_sql_error(caught.value)
    assert len(connection.catalog) == 1
    assert connection.schema == {"payload": _DOMAIN + "[]"}
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize(
    "row",
    [
        None,
        (),
        (0, 0, "varchar"),
        (-1, 0, "varchar", _BASE),
        (0, -1, "varchar", _BASE),
        (True, 0, "varchar", _BASE),
        (0, True, "varchar", _BASE),
        (1043, 1043, "varchar", _BASE),
        (0, 0, None, _BASE),
        (0, 0, "", _BASE),
        (0, 0, "varchar", None),
        (0, 0, "varchar", " "),
    ],
)
def test_missing_or_malformed_pg_type_metadata_never_produces_a_bind_type(row):
    types = _domain_types()
    types[_ARRAY_OID] = row
    connection = _Connection(types=types)
    with pytest.raises(NativeAssignmentError) as caught:
        _bind(connection)
    assert is_non_retryable_sql_error(caught.value)
    assert connection.schema == {"payload": _DOMAIN + "[]"}
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("case", ["cycle", "no-array", "missing-leaf", "too-deep"])
def test_unprovable_native_type_graphs_fail_without_guessing_or_unbounded_scans(case):
    types = _domain_types()
    if case == "cycle":
        types[_DOMAIN_OID] = (_ARRAY_OID, 0, "domain", _DOMAIN)
    elif case == "no-array":
        types[_ARRAY_OID] = (_VARCHAR_OID, 0, "not_an_array", _DOMAIN)
    elif case == "missing-leaf":
        types.pop(_VARCHAR_OID)
    else:
        types = {oid: (oid + 1, 0, "domain", _DOMAIN) for oid in range(9001, 9100)}
        types[_ARRAY_OID] = (0, 9003, "array", "array")
    connection = _Connection(types=types)
    with pytest.raises(NativeAssignmentError) as caught:
        _bind(connection)
    assert is_non_retryable_sql_error(caught.value)
    oids = [params[0] for _query, params in connection.catalog[1:]]
    assert len(oids) == len(set(oids))
    assert len(oids) <= 64
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize("code", ["42501", "08006"])
def test_catalog_errors_preserve_retry_policy_and_hide_private_server_values(code, capsys):
    failure = _DatabaseError(code)
    failure.analytics_toolkit_sql_stage_candidate = _STAGE
    connection = _Connection(failure=failure, close_failure=True)
    with pytest.raises(NativeAssignmentError) as caught:
        _bind(connection)
    assert caught.value.sqlstate == code
    assert caught.value.analytics_toolkit_sql_stage_candidate == _STAGE
    assert is_non_retryable_sql_error(caught.value) is (code == "42501")
    assert _SECRET not in str(caught.value)
    assert _SECRET not in capsys.readouterr().out
    assert all(cursor.closed for cursor in connection.cursors)


def test_catalog_cursor_close_failure_cannot_invalidate_a_completed_bind_resolution(capsys):
    connection = _Connection(close_failure=True)
    assert _bind(connection) == {"payload": _BASE + "[]"}
    assert len(connection.catalog) == 4
    output = capsys.readouterr().out
    assert "Cursor close failed" in output
    assert _SECRET not in output


@pytest.mark.parametrize("when", ["before", "attributes", "array", "leaf"])
def test_catalog_cancellation_stops_before_any_further_queries_or_population(when):
    scope = SqlCancellationScope()
    if when == "before":
        scope.request_cancel()

    def cancel(params):
        if (
            (when == "attributes" and len(params) == 3)
            or (when == "array" and params == (_ARRAY_OID,))
            or (when == "leaf" and params == (_VARCHAR_OID,))
        ):
            scope.request_cancel()

    connection = _Connection()
    connection.on_catalog = cancel
    with activate_cancellation_scope(scope), pytest.raises(AsyncSqlCancelled):
        _bind(connection)
    assert len(connection.catalog) == {"before": 0, "attributes": 1, "array": 2, "leaf": 4}[when]
    assert connection.statements == []
    assert all(cursor.closed for cursor in connection.cursors)

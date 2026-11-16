"""Signed-zero SQL adaptation stays at the final verified GP floating boundary."""

from __future__ import annotations

import importlib
import math
from copy import deepcopy
from decimal import Decimal

import numpy as np
import pytest
from psycopg2.extensions import AsIs, adapt


@pytest.fixture
def float_parameters():
    return importlib.import_module("analytics_toolkit.sql.backends.gp.float_parameters")


@pytest.mark.parametrize(
    ("native", "cast_type"),
    [
        ("REAL", "REAL"),
        ("float4", "REAL"),
        ("FLOAT(24)", "REAL"),
        ('pg_catalog."float4"', "REAL"),
        ("DOUBLE PRECISION", "FLOAT8"),
        ("float8", "FLOAT8"),
        ("FLOAT", "FLOAT8"),
        ("FLOAT(53)", "FLOAT8"),
        ('pg_catalog."float8"', "FLOAT8"),
    ],
)
def test_gp_float_negative_zero_uses_real_psycopg_native_input_without_changing_rows(
    float_parameters, native, cast_type
):
    values = [-0.0, 0.0, 1.25, -2.5, None]
    rows = [[value] for value in values]

    result = float_parameters.normalize_float_parameters(["value"], rows, {"value": native})

    assert result is not rows
    assert all(type(row) is tuple for row in result)
    parameter = result[0][0]
    assert isinstance(parameter, AsIs)
    assert adapt(parameter).getquoted() == f"'-0.0'::{cast_type}".encode()
    assert all(result[index][0] is value for index, value in enumerate(values) if index)
    assert all(type(row) is list for row in rows)
    assert all(rows[index][0] is value for index, value in enumerate(values))
    assert type(rows[0][0]) is float
    assert math.copysign(1, rows[0][0]) == -1


@pytest.mark.parametrize("dtype", [float, np.float32, np.float64])
def test_gp_float_zero_sign_is_not_inferred_from_numeric_equality(float_parameters, dtype):
    negative = dtype(-0.0)
    positive = dtype(0.0)
    rows = [(negative,), (positive,)]

    result = float_parameters.normalize_float_parameters(["value"], rows, {"value": "REAL"})

    assert isinstance(result[0][0], AsIs)
    assert result[0][0].getquoted() == b"'-0.0'::REAL"
    assert result[1][0] is positive
    assert math.copysign(1, negative) == -1
    assert math.copysign(1, positive) == 1


@pytest.mark.parametrize(
    "native", [None, {}, {"value": None}, {"value": "NUMERIC"}, {"other": "REAL"}]
)
def test_gp_float_parameters_require_exact_target_metadata(float_parameters, native):
    negative = -0.0
    rows = [(negative,)]

    result = float_parameters.normalize_float_parameters(["value"], rows, native)

    assert result is not rows
    assert result[0][0] is negative
    assert type(result[0][0]) is float


@pytest.mark.parametrize(
    "native",
    ["INTEGER", "NUMERIC(38,9)", "TEXT", "JSONB", "public.float8", '"Float8"', "public.float8[]"],
)
def test_gp_float_parameters_do_not_guess_custom_or_nonfloating_storage(float_parameters, native):
    negative = -0.0
    nested = [[negative]]
    rows = [(negative, nested)]

    result = float_parameters.normalize_float_parameters(
        ["scalar", "nested"], rows, {"scalar": native, "nested": native}
    )

    assert result[0][0] is negative
    assert result[0][1] is nested


@pytest.mark.parametrize(
    "value", [0, False, Decimal("-0"), "-0.0", b"-0.0", None, float("inf"), float("nan")]
)
def test_gp_float_parameter_adapter_is_not_a_scalar_conversion_policy(float_parameters, value):
    result = float_parameters.normalize_float_parameters(["value"], [(value,)], {"value": "REAL"})

    assert result[0][0] is value


@pytest.mark.parametrize("native", ["REAL[]", "REAL[][]", "pg_catalog._float4"])
def test_gp_float_array_zero_adaptation_uses_runtime_rank_and_actual_driver(
    float_parameters, native
):
    values = [
        [[-0.0, 0.0], [None, -0.0]],
        [[-0.0, 1.0], [0.0, -0.0]],
    ]
    original = deepcopy(values)

    result = float_parameters.normalize_float_parameters(["value"], [(values,)], {"value": native})

    adapted = result[0][0]
    assert type(adapted) is list
    assert adapted is not values
    assert all(type(layer) is list for layer in adapted)
    assert all(type(row) is list for layer in adapted for row in layer)
    assert isinstance(adapted[0][0][0], AsIs)
    assert isinstance(adapted[0][1][1], AsIs)
    assert isinstance(adapted[1][0][0], AsIs)
    assert isinstance(adapted[1][1][1], AsIs)
    assert adapt(adapted).getquoted().count(b"'-0.0'::REAL") == 4
    assert adapted[0][0][1] is values[0][0][1]
    assert adapted[0][1][0] is None
    assert values == original
    assert math.copysign(1, values[0][0][0]) == -1
    assert all(not isinstance(value, AsIs) for layer in values for row in layer for value in row)


def test_gp_float_array_adapter_retains_tuple_containers_and_skips_scalar_container_values(
    float_parameters,
):
    values = ((-0.0, None), (0.0, -0.0))

    result = float_parameters.normalize_float_parameters(
        ["array", "scalar"], [(values, values)], {"array": "float8[]", "scalar": "FLOAT8"}
    )

    adapted = result[0][0]
    assert type(adapted) is tuple
    assert all(type(row) is tuple for row in adapted)
    assert adapted is not values
    assert adapted[0][0].getquoted() == adapted[1][1].getquoted() == b"'-0.0'::FLOAT8"
    assert adapted[0][1] is None
    assert adapted[1][0] is values[1][0]
    assert result[0][1] is values


def test_gp_float_parameter_replay_is_idempotent_and_preserves_column_identity(float_parameters):
    document = {"value": -0.0}
    rows = [(-0.0, document, -0.0)]
    columns = [" spaced ", "json", 'quoted"column']
    types = {" spaced ": "REAL", "json": "JSONB", 'quoted"column': "FLOAT8"}

    first = float_parameters.normalize_float_parameters(columns, rows, types)
    second = float_parameters.normalize_float_parameters(columns, first, types)

    assert second[0][0] is first[0][0]
    assert second[0][2] is first[0][2]
    assert first[0][0].getquoted() == b"'-0.0'::REAL"
    assert first[0][2].getquoted() == b"'-0.0'::FLOAT8"
    assert first[0][1] is second[0][1] is document
    assert math.copysign(1, rows[0][0]) == -1
    assert math.copysign(1, document["value"]) == -1


def test_gp_float_parameter_adapter_keeps_extra_row_cells_for_existing_width_validation(
    float_parameters,
):
    extra = object()

    result = float_parameters.normalize_float_parameters(
        ["value"], [(-0.0, extra)], {"value": "REAL"}
    )

    assert len(result[0]) == 2
    assert isinstance(result[0][0], AsIs)
    assert result[0][1] is extra
    assert float_parameters.normalize_float_parameters(["value"], [], {"value": "REAL"}) == []

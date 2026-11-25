"""Render native partition values without Python repr or lossy numeric literals."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from numbers import Integral, Real
from struct import pack, unpack
from typing import Any
from uuid import UUID

from .dialect import sql_string_literal

_MESSAGE = "Selected input has no exact supported ClickHouse partition expression."
_DECIMAL256_PRECISION = 76


class ClickHousePartitionValueError(ValueError):
    analytics_toolkit_sql_retry_safe = False


def _integer_literal(value: Integral) -> str:
    integer = int(value)
    if -(2**63) <= integer <= 2**64 - 1:
        return str(integer)
    if -(2**255) <= integer <= 2**255 - 1:
        return f"toInt256({sql_string_literal(str(integer))})"
    if 0 <= integer <= 2**256 - 1:
        return f"toUInt256({sql_string_literal(str(integer))})"
    raise ClickHousePartitionValueError(_MESSAGE)


def _decimal_literal(value: Decimal) -> str:
    # Defer exact partition-value dependencies.
    from analytics_toolkit.sql.core.exact_values import decimal_shape  # noqa: PLC0415

    precision, scale = decimal_shape([value])
    if precision > _DECIMAL256_PRECISION:
        raise ClickHousePartitionValueError(_MESSAGE)
    return f"toDecimal256({sql_string_literal(format(value, 'f'))}, {scale})"


def _numeric_literal(value: Any) -> str | None:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Integral):
        return _integer_literal(value)
    if isinstance(value, Real):
        bits = unpack("<Q", pack("<d", float(value)))[0]
        return f"reinterpretAsFloat64(toUInt64({sql_string_literal(str(bits))}))"
    if isinstance(value, Decimal):
        return _decimal_literal(value)
    return None


def _render_partition_value(value: Any) -> str:
    # Defer exact partition-value dependencies.
    from analytics_toolkit.sql.core.exact_values import ExactTemporal  # noqa: PLC0415

    numeric = _numeric_literal(value)
    if numeric is not None:
        return numeric
    if isinstance(value, tuple):
        return "tuple(" + ", ".join(_render_partition_value(item) for item in value) + ")"
    if isinstance(value, str):
        return sql_string_literal(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"unhex({sql_string_literal(bytes(value).hex())})"
    if isinstance(value, (UUID, IPv4Address, IPv6Address)):
        function = (
            "toUUID"
            if isinstance(value, UUID)
            else "toIPv4"
            if isinstance(value, IPv4Address)
            else "toIPv6"
        )
        return f"{function}({sql_string_literal(str(value))})"
    if isinstance(value, (date, ExactTemporal)):
        # Defer exact partition-value dependencies.
        from .key_literals import render_transfer_literal  # noqa: PLC0415

        return render_transfer_literal(None, value)
    raise ClickHousePartitionValueError(_MESSAGE)


def render_partition_value(value: Any) -> str:
    """Keep recursive native operands exact before any partition mutation is sent."""
    try:
        return _render_partition_value(value)
    except ClickHousePartitionValueError:
        raise
    except (TypeError, ValueError, OverflowError):
        raise ClickHousePartitionValueError(_MESSAGE) from None

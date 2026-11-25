"""Known native Trino map casts over the installed SDK's actual parameter wire."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from tests.sql._support.trino_time_values import native_time_target


@dataclass(frozen=True)
class ClockMapCast:
    wire: str
    value: dict | None = None
    native_type: str | None = None


def native_clock_map_target(monkeypatch, native_type, *, original_rows, expected_cast=None):
    database = native_time_target(monkeypatch, native_type)
    database.table_rows[database.target] = deepcopy(original_rows)
    database.map_casts = []

    def native_cast(wire):
        assert expected_cast is not None, "Uncertifiable map reached native insertion."
        cast_type = expected_cast.native_type or native_type
        assert f"CAST(? AS {cast_type})" in database.statements[-1][0]
        assert wire == expected_cast.wire
        database.map_casts.append(wire)
        if expected_cast.value is None:
            message = "Trino rejects duplicate map keys after casting their clock components."
            raise ValueError(message)
        return deepcopy(expected_cast.value)

    monkeypatch.setattr(database, "decode_cast", native_cast)
    return database

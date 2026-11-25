"""Independently modeled native GP temporal assignments over real psycopg binds."""

from __future__ import annotations

import datetime as dt
from copy import deepcopy

from analytics_toolkit.sql.backends.native_scalars import ExactInterval
from analytics_toolkit.sql.dml.transfer.runtime import retry

from tests.sql._support.gp_dates import _DateConnection
from tests.sql._support.gp_timestamp_text import TimestampDatabase, _TimestampInputError
from tests.sql._support.load_table import load_df_module


class NativeTemporalDatabase(TimestampDatabase):
    def __init__(self, native):
        super().__init__(native)
        self.settings["intervalstyle"] = "postgres"
        seed = {
            "DATE": dt.date(2001, 1, 2),
            "TIME": dt.time(1, 2, 3),
            "INTERVAL": ExactInterval(0, 1, 0),
        }[native]
        self.target.rows = [{"payload": seed}]
        self.old_rows = deepcopy(self.target.rows)

    @staticmethod
    def assign(value, native_type):
        if value is None:
            return None
        if native_type.upper() in {"TEXT", "VARCHAR"}:
            return value
        try:
            return _temporal_input(value, native_type.upper())
        except (TypeError, ValueError):
            pass
        message = "Native temporal input rejected."
        raise _TimestampInputError(message)


def _temporal_input(value, native):
    if native == "DATE":
        return _date_input(value)
    if native == "TIME":
        return dt.time.fromisoformat(value) if isinstance(value, str) else value
    if native == "INTERVAL":
        return _interval_input(value)
    message = "Unsupported native temporal fixture type."
    raise ValueError(message)


def _interval_input(value):
    if isinstance(value, dt.timedelta):
        return ExactInterval(0, value.days, value.seconds * 10**6 + value.microseconds)
    if isinstance(value, ExactInterval):
        return value
    # Independently model only the verified native subset, not the toolkit parser.
    if value in {"1 month", "1 mon"}:
        return ExactInterval(1, 0, 0)
    if value == "30 days":
        return ExactInterval(0, 30, 0)
    message = "Native interval input rejected."
    raise ValueError(message)


def _date_input(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = value.strip(" \t\r\n\v\f")
    if len(text) == 8 and text.isascii() and text.isdigit():
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:]))
    if text == "January 8, 1999":
        return dt.date(1999, 1, 8)
    return dt.datetime.fromisoformat(text).date() if " " in text else dt.date.fromisoformat(text)


def native_temporal_target(monkeypatch, native):
    database = NativeTemporalDatabase(native)

    def connect(_alias):
        connection = _DateConnection(database)
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    monkeypatch.setattr(retry, "get_sql_connection", connect)
    return database

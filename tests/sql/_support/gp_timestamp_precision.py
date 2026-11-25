"""GP typmod arithmetic over actual psycopg2 formatting, without a database."""

from __future__ import annotations

import datetime as dt
import re

from tests.sql._support.gp_dates import _DateConnection
from tests.sql._support.gp_timestamp_text import TimestampDatabase
from tests.sql._support.load_table import load_df_module


class RoundingTimestampDatabase(TimestampDatabase):
    @staticmethod
    def assign(value, native_type):
        assigned = TimestampDatabase.assign(value, native_type)
        if assigned is None:
            return None
        match = re.fullmatch(r"TIMESTAMP(?:\(([0-6])\))?", native_type, flags=re.IGNORECASE)
        assert match is not None
        precision = int(match[1]) if match[1] is not None else 6
        # GP AdjustTimestampForTypmod uses signed microseconds from 2000-01-01.
        # https://github.com/greenplum-db/gpdb-archive/blob/main/src/backend/utils/adt/timestamp.c
        epoch = dt.datetime.fromisoformat("2000-01-01T00:00:00.000000")
        delta = assigned - epoch
        ticks = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        step = 10 ** (6 - precision)
        rounded = ((abs(ticks) + step // 2) // step) * step
        return epoch + dt.timedelta(microseconds=-rounded if ticks < 0 else rounded)


def timestamp_precision_target(monkeypatch, native_type):
    database = RoundingTimestampDatabase(native_type)
    database.settings["intervalstyle"] = "postgres"

    def connect(_alias):
        connection = _DateConnection(database)
        connection.pending_bound_rows = []
        database.opened.append(connection)
        return connection

    monkeypatch.setattr(load_df_module, "get_sql_connection", connect)
    return database

import pandas as pd
from analytics_toolkit import (
    ab_utils as ab,
)
from analytics_toolkit import (
    dates as dt,
)
from analytics_toolkit import (
    datetime as dttm,
)
from analytics_toolkit import (
    excel,
    sql,
    sql_format,
)
from analytics_toolkit.general import (
    from_here,
    get_time_print_sink,
    here,
    read_file_here,
    set_connections_path,
    time_print,
    write_file,
)

import atk


def test_atk_exports_exact_requested_shortcuts() -> None:
    expected = {
        "ab": ab,
        "dt": dt,
        "dttm": dttm,
        "excel": excel,
        "from_here": from_here,
        "get_time_print_sink": get_time_print_sink,
        "here": here,
        "pd": pd,
        "read_file_here": read_file_here,
        "set_connections_path": set_connections_path,
        "sql": sql,
        "sql_format": sql_format,
        "time_print": time_print,
        "write_file": write_file,
    }

    assert set(atk.__all__) == set(expected)
    assert {name for name in vars(atk) if name.isidentifier() and not name.startswith("_")} == set(
        expected
    )
    for name, value in expected.items():
        assert getattr(atk, name) is value

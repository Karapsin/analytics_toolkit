from __future__ import annotations

from datetime import datetime
import inspect
from io import StringIO

import pytest

import analytics_toolkit.general as general_module
import analytics_toolkit.sql as sql_module
from analytics_toolkit.general import (
    get_time_print_level,
    get_time_print_sink,
    set_time_print_clock,
    set_connections_path,
    set_time_print_level,
    set_time_print_sink,
    time_print,
    time_print_context,
)


FIXED_TIME = datetime(2026, 6, 3, 18, 0, 0)


@pytest.fixture(autouse=True)
def reset_time_print_state() -> None:
    set_time_print_level("info")
    set_time_print_sink("print")
    set_time_print_clock(lambda: FIXED_TIME)
    try:
        yield
    finally:
        set_time_print_level("info")
        set_time_print_sink("print")
        set_time_print_clock(None)


def test_time_print_preserves_legacy_output(capsys: pytest.CaptureFixture[str]) -> None:
    time_print("starting load")

    assert capsys.readouterr().out == "[2026-06-03 18:00:00] starting load\n"


def test_time_print_keyword_only_options_have_compatible_defaults() -> None:
    signature = inspect.signature(time_print)

    assert list(signature.parameters) == [
        "message",
        "level",
        "enabled",
        "operation",
        "connection",
        "backend",
        "phase",
        "task_id",
        "stream",
    ]
    for parameter_name in [
        "level",
        "enabled",
        "operation",
        "connection",
        "backend",
        "phase",
        "task_id",
        "stream",
    ]:
        parameter = signature.parameters[parameter_name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is not inspect.Parameter.empty

    assert signature.parameters["level"].default == "info"
    assert signature.parameters["enabled"].default is True
    assert signature.parameters["operation"].default is None
    assert signature.parameters["connection"].default is None
    assert signature.parameters["backend"].default is None
    assert signature.parameters["phase"].default is None
    assert signature.parameters["task_id"].default is None
    assert signature.parameters["stream"].default is None


def test_time_print_filters_by_level(capsys: pytest.CaptureFixture[str]) -> None:
    set_time_print_level("warning")

    time_print("debug message", level="debug")
    time_print("info message", level="info")
    time_print("warning message", level="warning")
    time_print("error message", level="error")

    output = capsys.readouterr().out
    assert "debug message" not in output
    assert "info message" not in output
    assert "warning message" in output
    assert "error message" in output
    assert get_time_print_level() == "warning"


def test_time_print_validates_levels() -> None:
    with pytest.raises(ValueError, match="level must be one of"):
        set_time_print_level("notice")

    with pytest.raises(ValueError, match="level must be one of"):
        time_print("message", level="notice")

    with pytest.raises(TypeError, match="level must be a string"):
        time_print("message", level=1)  # type: ignore[arg-type]


def test_time_print_can_emit_through_logging(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_time_print_sink("logging")

    with caplog.at_level("INFO", logger="analytics_toolkit"):
        time_print(
            "loaded rows",
            operation="load_df",
            connection="airflow_ch",
            backend="ch",
            phase="insert",
        )

    assert capsys.readouterr().out == ""
    assert [
        (record.name, record.levelname, record.getMessage())
        for record in caplog.records
    ] == [
        (
            "analytics_toolkit",
            "INFO",
            "[load_df] [airflow_ch/ch] [insert] loaded rows",
        )
    ]
    assert get_time_print_sink() == "logging"


def test_time_print_logging_sink_respects_levels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_time_print_sink("logging")
    set_time_print_level("warning")

    with caplog.at_level("DEBUG", logger="analytics_toolkit"):
        time_print("debug message", level="debug")
        time_print("info message", level="info")
        time_print("warning message", level="warning")
        time_print("error message", level="error")

    assert [record.levelname for record in caplog.records] == ["WARNING", "ERROR"]
    assert [record.getMessage() for record in caplog.records] == [
        "warning message",
        "error message",
    ]


def test_time_print_validates_sink() -> None:
    with pytest.raises(ValueError, match="sink must be one of"):
        set_time_print_sink("airflow")

    with pytest.raises(TypeError, match="sink must be a string"):
        set_time_print_sink(1)  # type: ignore[arg-type]


def test_time_print_enabled_flag_suppresses_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    time_print("hidden", enabled=False)

    assert capsys.readouterr().out == ""


def test_time_print_explicit_streams_override_logging_sink_with_timestamps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    buffer = StringIO()
    set_time_print_sink("logging")

    time_print("stdout message", stream="stdout")
    time_print("stderr message", stream="stderr")
    time_print("buffer message", stream=buffer)

    captured = capsys.readouterr()
    assert captured.out == "[2026-06-03 18:00:00] stdout message\n"
    assert captured.err == "[2026-06-03 18:00:00] stderr message\n"
    assert buffer.getvalue() == "[2026-06-03 18:00:00] buffer message\n"


def test_time_print_validates_stream() -> None:
    with pytest.raises(ValueError, match="stream must be"):
        time_print("message", stream="file")

    with pytest.raises(TypeError, match="write method"):
        time_print("message", stream=object())  # type: ignore[arg-type]


def test_time_print_validates_and_uses_injected_clock(
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_time_print_clock(lambda: datetime(2026, 6, 4, 8, 30, 15))

    time_print("clocked")

    assert capsys.readouterr().out == "[2026-06-04 08:30:15] clocked\n"

    set_time_print_clock(lambda: "2026-06-04")  # type: ignore[return-value]
    with pytest.raises(TypeError, match="clock must return a datetime"):
        time_print("bad clock")

    with pytest.raises(TypeError, match="clock must be callable"):
        set_time_print_clock("now")  # type: ignore[arg-type]


def test_time_print_context_applies_and_restores_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with time_print_context(
        operation="load_df",
        connection="gp_prod",
        backend="gp",
        phase="insert",
    ):
        time_print("writing rows")

    time_print("done")

    assert capsys.readouterr().out.splitlines() == [
        "[2026-06-03 18:00:00] [load_df] [gp_prod/gp] [insert] writing rows",
        "[2026-06-03 18:00:00] done",
    ]


def test_time_print_context_nests_and_explicit_kwargs_override(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with time_print_context(operation="transfer_table", connection="trino_prod"):
        with time_print_context(phase="read"):
            time_print("reading")
            time_print("creating", operation="load_df", backend="ch", phase="write")

    assert capsys.readouterr().out.splitlines() == [
        "[2026-06-03 18:00:00] [transfer_table] [trino_prod] [read] reading",
        "[2026-06-03 18:00:00] [load_df] [trino_prod/ch] [write] creating",
    ]


def test_time_print_context_includes_task_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with time_print_context(task_id="orchestrator"):
        time_print("running")

    assert (
        capsys.readouterr().out
        == "[2026-06-03 18:00:00] [task_id=orchestrator] running\n"
    )


def test_time_print_context_prefix_order_is_stable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with time_print_context(
        operation="operation_alpha",
        connection="analytics_conn",
        backend="gp",
        phase="read",
        task_id="copy_table",
    ):
        time_print("processing")

    assert (
        capsys.readouterr().out
        == (
            "[2026-06-03 18:00:00] [operation_alpha] [analytics_conn/gp] "
            "[read] [task_id=copy_table] processing\n"
        )
    )


def test_time_print_public_reexports_are_preserved() -> None:
    assert general_module.time_print is time_print
    assert general_module.set_time_print_level is set_time_print_level
    assert general_module.get_time_print_level is get_time_print_level
    assert general_module.set_time_print_sink is set_time_print_sink
    assert general_module.get_time_print_sink is get_time_print_sink
    assert general_module.set_time_print_clock is set_time_print_clock
    assert general_module.set_connections_path is set_connections_path
    assert "set_connections_path" in general_module.__all__
    assert general_module.time_print_context is time_print_context
    assert sql_module.time_print is time_print
    assert sql_module.set_time_print_sink is set_time_print_sink
    assert sql_module.get_time_print_sink is get_time_print_sink

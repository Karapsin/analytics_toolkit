from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.segment import Segment, Segments
from rich.style import Style

from agent_tools import sql_explorer_export_observer as observer


def _console(spaces: str = " ") -> Console:
    console = Console(file=StringIO(), record=True, width=20, height=5, color_system="truecolor")
    console.print(
        Segments(
            [
                Segment("A", Style(color="#ff0000")),
                Segment(spaces, Style(color="#eeeeee", underline=True, dim=True)),
                Segment("B\nsecond row\n", Style(color="#0000ff")),
            ]
        ),
        end="",
    )
    return console


@pytest.mark.parametrize("spaces", [" ", "    "])
@pytest.mark.parametrize("clear", [False, True])
def test_real_rich_recording_is_observed_without_changing_export_or_clear(
    spaces: str, clear: bool
) -> None:
    console = _console(spaces)
    reference = _console(spaces)
    original_buffer = tuple(console._record_buffer)
    expected = reference.export_svg(title="observer", unique_id="observer", clear=clear)
    original_method = Console.export_svg
    with observer.observe_export() as result:
        actual = console.export_svg(title="observer", unique_id="observer", clear=clear)
    assert actual == expected
    assert Console.export_svg is original_method
    assert tuple(console._record_buffer) == tuple(reference._record_buffer)
    if not clear:
        assert tuple(console._record_buffer) == original_buffer
    assert len(result["invocations"]) == 1
    invocation = result["invocations"][0]
    assert invocation["raw_svg_sha256"] == sha256(actual.encode("utf-8")).hexdigest()
    records = invocation["recording"]["segments"]
    assert "".join(record["text"] for record in records) == "".join(
        segment.text for segment in original_buffer
    )
    decorated = [record for record in records if record["text"] == spaces]
    assert decorated
    assert all(record["style"]["flags"]["underline"] for record in decorated)
    assert all(record["style"]["flags"]["dim"] for record in decorated)
    before = deepcopy(invocation["recording"])
    observer.map_export_spans(result, actual)
    assert invocation["recording"] == before
    mappings = invocation["underline_span_mapping"]
    assert mappings
    assert all("missing" not in mapping for mapping in mappings)
    assert any(mapping["stream_part"]["text"] == spaces for mapping in mappings)
    # Absence is an observation of this installed exporter, not a cross-version premise.
    for mapping in mappings:
        assert mapping["line_clip"].endswith("-line-0")
        assert mapping["cell_width"] > 0
        assert "candidates" in mapping
    assert result["dependencies"]["versions"]["rich"] != "unavailable"


def test_observer_forwards_original_exception_once_and_restores_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("export failed")
    calls = []

    def failing(console: Console, *args: object, **kwargs: object) -> str:
        calls.append((console, args, kwargs))
        raise error

    monkeypatch.setattr(Console, "export_svg", failing)
    console = _console()
    original_buffer = tuple(console._record_buffer)
    with pytest.raises(RuntimeError) as caught, observer.observe_export() as result:
        console.export_svg(clear=False)
    assert caught.value is error
    assert Console.export_svg is failing
    assert calls == [(console, (), {"clear": False})]
    assert tuple(console._record_buffer) == original_buffer
    assert not result["complete"]
    assert result["invocations"][0]["export_exception"] == "RuntimeError"


def test_unavailable_recording_does_not_replace_original_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Console, "export_svg", lambda _console, **_kwargs: "<svg/>")
    fake = SimpleNamespace()
    with observer.observe_export() as result:
        assert Console.export_svg(fake) == "<svg/>"
    assert result["invocations"][0]["missing"] == "recording:KeyError"
    assert not result["complete"]


def test_segment_and_aggregate_limits_report_incomplete_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = _console()
    before = tuple(console._record_buffer)
    monkeypatch.setattr(observer, "MAX_SEGMENTS", 1)
    with observer.observe_export() as result:
        console.export_svg(clear=False)
    assert tuple(console._record_buffer) == before
    assert "segment_limit" in result["invocations"][0]["recording"]["missing"]
    assert not result["complete"]
    monkeypatch.setattr(observer, "MAX_OBSERVATION_BYTES", 16)
    bounded = observer.bound_observation(result)
    assert not bounded["complete"]
    assert bounded["missing"] == ["observation_byte_limit"]
    assert "invocations" not in bounded
    assert bounded["invocation_count"] == 1


def test_no_export_and_missing_line_mapping_are_explicitly_incomplete() -> None:
    with observer.observe_export() as empty:
        pass
    assert not empty["complete"]
    assert empty["missing"] == ["expected_one_recording_export"]
    console = _console()
    with observer.observe_export() as result:
        console.export_svg(clear=False)
    observer.map_export_spans(result, "<svg/>")
    assert not result["complete"]
    assert "ambiguous_or_absent_line_clip" in result["missing"]

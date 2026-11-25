"""Adaptive HTTP profiling cannot certify a client-limited input prefix."""

from __future__ import annotations

from tests.sql._support.http_limit_transfer import install_limited_http_transfer


def test_public_adaptive_http_source_disables_underlying_limit_through_lazy_submission(monkeypatch):
    extreme = 2**255 - 1
    scenario, source, raw = install_limited_http_transfer(monkeypatch, [0, extreme])

    result = scenario.run(metadata=True)

    assert result.rows == result.metadata.input_count == 2
    assert scenario.loaded == [[("0",), (str(extreme),)]]
    assert scenario.schemas == [{"id": "VARCHAR"}]
    assert scenario.read_count == scenario.source_open_count == 1
    assert raw.limits_seen == [0]
    assert len(raw.queries) == 1
    assert raw.query_limit == source.query_limit == 1
    assert raw.response_close_calls == 1
    assert raw.close_calls == 1
    assert scenario.events.index(f"complete:{scenario.options.source_sql}") < scenario.events.index(
        "create_target"
    )

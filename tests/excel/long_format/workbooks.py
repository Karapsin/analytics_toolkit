"""Workbook helpers must retain layout and always release resources."""

from __future__ import annotations

import pytest

from tests.excel._support import workbooks


def test_workbook_context_closes_on_assertion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []

    class Workbook:
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(workbooks, "load_workbook", lambda *args, **kwargs: Workbook())
    message = "wrong cell"
    with pytest.raises(AssertionError, match="wrong cell"), workbooks.report_workbook(
        "unused.xlsx"
    ):
        raise AssertionError(message)
    assert closed == [True]


def test_report_frame_overrides_are_explicit_and_independent() -> None:
    first = workbooks.report_frame("users", [100, 110], qr_group=["A", "B"])
    first.loc[0, "value"] = 0
    second = workbooks.report_frame("users", [100, 110])
    assert second["value"].tolist() == [100, 110]
    assert second["qr_group"].tolist() == ["ALL", "ALL"]
    assert second.columns.tolist() == ["metric", "ab_group", "qr_group", "start_dt", "value"]

"""Native String values preserve the selected text or binary encoder mode."""

from __future__ import annotations

from copy import deepcopy
from struct import pack

import pytest

from tests.sql.backends.ch._support.native_string_insert import native_string_target


@pytest.mark.parametrize(
    ("native", "value", "wire"),
    [
        ("Array(String)", ["Ω"], pack("<Q", 1) + b"\x02\xce\xa9"),
        ("Tuple(String)", ("Ω",), b"\x02\xce\xa9"),
        ("Map(String, String)", {"Ω": "λ"}, pack("<Q", 1) + b"\x02\xce\xa9\x02\xce\xbb"),
        ("Array(Nullable(String))", [None, "Ω"], pack("<Q", 2) + b"\x01\x00\x00\x02\xce\xa9"),
    ],
)
@pytest.mark.parametrize("strings_as_bytes", [False, True])
def test_native_binary_string_insert_encodes_nested_unicode_under_selected_bytes_mode(
    monkeypatch, native, value, wire, strings_as_bytes
):
    store, endpoint = native_string_target(monkeypatch, {"payload": "String", "labels": native})
    store.rows.clear()
    endpoint.raw.client_settings["strings_as_bytes"] = strings_as_bytes
    rows = [(b"\xff", value)]
    original = deepcopy(rows)

    assert (
        endpoint.client.insert("sandbox.events", rows, ["payload", "labels"], ["String", native])
        == 1
    )

    assert store.wire_columns == [("payload", "String", b"\x01\xff"), ("labels", native, wire)]
    assert rows == original
    assert endpoint.insert_attempts == 1

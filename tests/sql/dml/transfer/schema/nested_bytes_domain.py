"""Backend-local physical String identity must survive nested validation."""

from __future__ import annotations

import pytest

from tests.sql._support.adaptive_transfer import install


@pytest.mark.parametrize(("backend", "target"), [("gp", "BYTEA[]"), ("trino", "ARRAY(VARBINARY)")])
@pytest.mark.parametrize("explicit", [False, True])
def test_public_transfer_preserves_nested_ch_physical_strings_to_binary(
    monkeypatch, backend, target, explicit
):
    values = [["ascii", "é", b"\xff\x00", None], []]
    options = {"table_schema": {"id": target}} if explicit else {}
    scenario = install(
        monkeypatch,
        native_type="Array(Nullable(String))",
        rows=[(value,) for value in values],
        to_db_key=backend,
        to_db_backend=backend,
        write_mode="append",
        **options,
    )
    scenario.existing_types = {"id": target}

    assert scenario.run() == 2

    assert scenario.loaded == [[([b"ascii", "é".encode(), b"\xff\x00", None],), ([],)]]
    assert scenario.read_count == 1
    assert scenario.existing_types == {"id": target}
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index("load")

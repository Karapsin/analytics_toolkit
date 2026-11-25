from __future__ import annotations

from typing import Any

import pytest

from tests.sql._support.adaptive_transfer import (
    Scenario,
    install,
    require_streaming_fast_path,
    track_spools,
)


def physical_ch_string(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    values: list[Any],
    existing_type: str | None = None,
) -> Scenario:
    scenario = install(
        monkeypatch,
        native_type="Nullable(String)" if None in values else "String",
        rows=[(value,) for value in values],
        to_db_key=backend,
        to_db_backend=backend,
        target_table="sandbox.events",
        write_mode="append",
        replace_target_table=False,
    )
    if existing_type is not None:
        scenario.existing_types = {"id": existing_type}
    return scenario


@pytest.mark.parametrize(("backend", "native_type"), [("gp", "BYTEA"), ("trino", "VARBINARY")])
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([None, b"\xff\x00", None], [None, b"\xff\x00", None]),
        (["foo", "é", b"\xff\x00"], [b"foo", "é".encode(), b"\xff\x00"]),
    ],
)
def test_physical_ch_strings_use_exact_native_binary_for_arbitrary_or_mixed_bytes(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    native_type: str,
    values: list[Any],
    expected: list[Any],
) -> None:
    scenario = physical_ch_string(monkeypatch, backend, values)
    paths = track_spools(monkeypatch)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.schemas == [{"id": native_type}]
    assert scenario.loaded == [[(value,) for value in expected]]
    assert scenario.read_count == 1
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(
    ("values", "native_type", "expected"),
    [
        (["with\x00nul"], "BYTEA", [b"with\x00nul"]),
        (["ordinary", "é"], "TEXT", ["ordinary", "é"]),
    ],
)
def test_ch_string_profiles_before_new_gp_creation_and_preserves_the_actual_text_domain(
    monkeypatch: pytest.MonkeyPatch,
    values: list[str],
    native_type: str,
    expected: list[Any],
) -> None:
    scenario = physical_ch_string(monkeypatch, "gp", values)

    result = scenario.run(metadata=True)

    assert result.rows == len(values)
    assert result.metadata.input_count == len(values)
    assert scenario.schemas == [{"id": native_type}]
    assert scenario.loaded == [[(value,) for value in expected]]
    assert scenario.events.index("complete:SELECT id FROM source") < scenario.events.index(
        "create_target"
    )


@pytest.mark.parametrize("values", [[b"\xff\x00"], ["with\x00nul"], ["ordinary", b"\xff"]])
def test_existing_gp_text_rejects_incompatible_physical_strings_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
    values: list[Any],
) -> None:
    scenario = physical_ch_string(monkeypatch, "gp", values, "TEXT")
    paths = track_spools(monkeypatch)

    with pytest.raises(ValueError, match="authoritative Greenplum text"):
        scenario.run()

    assert scenario.read_count == 1
    assert "create_target" not in scenario.events
    assert not scenario.schemas
    assert not scenario.loaded
    assert scenario.finalizations == 0
    assert scenario.existing_types == {"id": "TEXT"}
    assert paths
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize(("backend", "native_type"), [("gp", "BYTEA"), ("trino", "VARBINARY")])
def test_existing_binary_target_accepts_the_ch_physical_string_domain_exactly(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    native_type: str,
) -> None:
    scenario = physical_ch_string(monkeypatch, backend, ["foo", "é", b"\xff\x00"], native_type)

    assert scenario.run() == 3

    assert scenario.schemas == [{"id": native_type}]
    assert scenario.loaded == [[(b"foo",), ("é".encode(),), (b"\xff\x00",)]]
    assert scenario.read_count == 1
    assert scenario.existing_types == {"id": native_type}


def test_native_gp_text_to_trino_varchar_keeps_the_metadata_proven_streaming_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = install(
        monkeypatch,
        native_type="TEXT",
        rows=[("ordinary",), ("é",)],
        from_db_key="gp",
        from_db_backend="gp",
    )
    require_streaming_fast_path(monkeypatch)

    assert scenario.run() == 2

    assert scenario.schemas == [{"id": "VARCHAR"}]
    assert scenario.loaded == [[("ordinary",), ("é",)]]
    assert scenario.events.index("create_target") < scenario.events.index(
        "read:SELECT id FROM source"
    )

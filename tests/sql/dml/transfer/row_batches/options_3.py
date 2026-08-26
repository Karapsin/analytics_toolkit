from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    pytest,
    transfer_api_module,
    transfer_concurrency_module,
)


@pytest.mark.parametrize(
    "target_batch_memory_mb",
    [0, -1, True, "64", float("nan"), float("inf")],
)
def test_transfer_options_validate_target_batch_memory(
    target_batch_memory_mb: Any,
) -> None:
    with pytest.raises(ValueError, match="target_batch_memory_mb"):
        transfer_api_module.build_transfer_options(
            from_db="gp",
            to_db="trino",
            from_sql="select id from source_table",
            to_table="sandbox.target",
            target_batch_memory_mb=target_batch_memory_mb,
        )


def test_transfer_rejects_concurrency_above_hard_cap() -> None:
    with pytest.raises(
        ValueError,
        match="effective transfer concurrency exceeds hard_concurrency_cap",
    ):
        transfer_concurrency_module.resolve_transfer_concurrency(
            concurrency=None,
            read_concurrency=8,
            write_concurrency=3,
            soft_concurrency_cap=None,
            hard_concurrency_cap=5,
            slice_count=2,
            direct_keyed=True,
        )


def test_transfer_rejects_hard_cap_before_connection_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        transfer_api_module,
        "get_connection_config",
        lambda _db_key: pytest.fail("connection config lookup must not run"),
    )

    with pytest.raises(ValueError, match="exceeds hard_concurrency_cap"):
        transfer_api_module.transfer_table(
            from_db="source",
            to_db="target",
            from_table="sandbox.source",
            to_table="sandbox.target",
            concurrency=6,
            hard_concurrency_cap=5,
            dry_run=True,
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("soft_concurrency_cap", 0),
        ("soft_concurrency_cap", True),
        ("soft_concurrency_cap", 1.5),
        ("hard_concurrency_cap", 0),
        ("hard_concurrency_cap", True),
        ("hard_concurrency_cap", None),
    ],
)
def test_transfer_rejects_invalid_concurrency_caps(name: str, value: Any) -> None:
    values = {"soft_concurrency_cap": None, "hard_concurrency_cap": 5}
    values[name] = value
    with pytest.raises(ValueError, match=rf"{name} must be a positive integer"):
        transfer_concurrency_module.resolve_transfer_concurrency(
            concurrency=1,
            read_concurrency=None,
            write_concurrency=None,
            **values,
            slice_count=None,
            direct_keyed=False,
        )


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
@pytest.mark.parametrize("name", ["concurrency", "read_concurrency", "write_concurrency"])
def test_transfer_rejects_invalid_concurrency_values(name: str, value: Any) -> None:
    values = {"concurrency": None, "read_concurrency": None, "write_concurrency": None}
    values[name] = value
    with pytest.raises(ValueError, match=rf"{name} must be a positive integer"):
        transfer_concurrency_module.resolve_transfer_concurrency(
            **values,
            slice_count=2,
            direct_keyed=True,
        )


def test_transfer_rejects_legacy_and_split_concurrency_conflict() -> None:
    with pytest.raises(
        ValueError,
        match=(
            "concurrency cannot be combined with read_concurrency or write_concurrency; "
            "use either the legacy combined setting or the split settings\\."
        ),
    ):
        transfer_api_module.transfer_table(
            from_db="source",
            to_db="target",
            from_table="sandbox.source",
            to_table="sandbox.target",
            concurrency=3,
            read_concurrency=6,
            write_concurrency=2,
            dry_run=True,
        )
    with pytest.raises(ValueError, match="concurrency cannot be combined"):
        transfer_concurrency_module.resolve_transfer_concurrency(
            concurrency=3,
            read_concurrency=6,
            write_concurrency=2,
            slice_count=4,
            direct_keyed=True,
        )


@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_transfer_rejects_non_boolean_ignore_source_staging(value: Any) -> None:
    with pytest.raises(ValueError, match="ignore_source_staging must be a boolean"):
        transfer_api_module.build_transfer_options(
            from_db="source",
            to_db="target",
            from_table="sandbox.source",
            to_table="sandbox.target",
            ignore_source_staging=value,
        )


def test_transfer_rejects_split_concurrency_outside_keyed_scope() -> None:
    with pytest.raises(ValueError, match="supported only for keyed transfers"):
        transfer_concurrency_module.resolve_transfer_concurrency(
            concurrency=None,
            read_concurrency=2,
            write_concurrency=None,
            slice_count=None,
            direct_keyed=False,
        )


@pytest.mark.parametrize(
    ("legacy", "read", "write", "expected"),
    [
        (None, None, None, (1, 1)),
        (3, None, None, (3, 3)),
        (None, 6, 2, (6, 2)),
        (None, 4, None, (4, 1)),
        (None, None, 3, (1, 3)),
    ],
)
def test_transfer_resolves_concurrency_modes(
    legacy: int | None,
    read: int | None,
    write: int | None,
    expected: tuple[int, int],
) -> None:
    resolved = transfer_concurrency_module.resolve_transfer_concurrency(
        concurrency=legacy,
        read_concurrency=read,
        write_concurrency=write,
        hard_concurrency_cap=6,
        slice_count=10,
        direct_keyed=True,
    )
    assert (resolved.effective_read, resolved.effective_write) == expected


def test_transfer_resolves_soft_and_hard_concurrency_caps() -> None:
    resolved = transfer_concurrency_module.resolve_transfer_concurrency(
        concurrency=None,
        read_concurrency=8,
        write_concurrency=3,
        soft_concurrency_cap=2,
        hard_concurrency_cap=5,
        slice_count=10,
        direct_keyed=True,
    )

    assert (resolved.requested_read, resolved.requested_write) == (8, 3)
    assert (resolved.soft_limited_read, resolved.soft_limited_write) == (2, 2)
    assert (resolved.effective_read, resolved.effective_write) == (2, 2)
    assert resolved.soft_concurrency_cap == 2
    assert resolved.hard_concurrency_cap == 5

    slice_limited = transfer_concurrency_module.resolve_transfer_concurrency(
        concurrency=None,
        read_concurrency=5,
        write_concurrency=4,
        soft_concurrency_cap=None,
        hard_concurrency_cap=5,
        slice_count=2,
        direct_keyed=True,
    )
    assert (slice_limited.soft_limited_read, slice_limited.soft_limited_write) == (5, 4)
    assert (slice_limited.effective_read, slice_limited.effective_write) == (2, 2)

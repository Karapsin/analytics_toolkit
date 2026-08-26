from __future__ import annotations

from tests.sql._support.transfer_ordinal import (
    Any,
    TransferStageState,
    _staged_options,
    load_stage,
    pd,
    pytest,
    resolve_internal_columns,
    staged_attempt,
)


def test_stage_creation_race_reallocates_and_hashed_prefix_is_stable(monkeypatch: Any) -> None:
    existence = iter([False, True, False])
    creates = 0

    def create(*_args: Any, **_kwargs: Any) -> None:
        nonlocal creates
        creates += 1
        if creates == 1:
            raise RuntimeError("duplicate table")

    monkeypatch.setattr(load_stage, "table_exists", lambda *_args, **_kwargs: next(existence))
    monkeypatch.setattr(load_stage, "_create_sql_table_with_connection", create)
    actual = load_stage.create_stage_table(
        "trino",
        object(),
        "sales.orders",
        pd.DataFrame({"id": [1]}),
        random_suffix="transferid__w00000",
        destination_hash="0123456789abcdef",
    )
    relation = actual.split(".")[-1].strip('"')
    assert relation[:-4].endswith("transferid__w00000")
    assert len(relation.encode()) <= 63
    assert load_stage.build_stage_table_prefix(
        "trino", "sales.orders", None, "0123456789abcdef"
    ) == load_stage.build_stage_table_prefix("gp", "sales.orders", None, "0123456789abcdef")

    monkeypatch.setattr(load_stage, "table_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        load_stage,
        "_create_sql_table_with_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("create failed")),
    )
    with pytest.raises(RuntimeError, match="create failed"):
        load_stage.create_stage_table(
            "trino",
            object(),
            "sales.orders",
            pd.DataFrame({"id": [1]}),
            destination_hash="0123456789abcdef",
        )


def test_worker_stage_creation_drops_ambiguous_candidate_after_success(
    monkeypatch: Any,
) -> None:
    options = _staged_options()
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    dropped: list[str] = []

    def create(*_args: Any, **kwargs: Any) -> str:
        callback = kwargs["on_stage_candidate"]
        callback("stage.ambiguous")
        callback("stage.worker_0")
        return "stage.worker_0"

    monkeypatch.setattr(staged_attempt, "create_stage_table", create)
    monkeypatch.setattr(
        staged_attempt,
        "cleanup_stage_table",
        lambda _backend, _connection, table, **_kwargs: dropped.append(table),
    )

    assert staged_attempt._create_worker_stages(
        options,
        {"connection": object()},
        state,
        worker_count=1,
    ) == ["stage.worker_0"]
    assert dropped == ["stage.ambiguous"]
    assert state.stage_table == "stage.worker_0"
    assert state.stage_tables == ["stage.worker_0"]


@pytest.mark.parametrize(
    ("ambiguous_candidate", "expected_registered"),
    [
        (None, ["stage.worker_0"]),
        ("stage.ambiguous_worker_1", ["stage.worker_0", "stage.ambiguous_worker_1"]),
    ],
)
def test_worker_stage_creation_registers_partial_and_ambiguous_candidates(
    monkeypatch: Any,
    ambiguous_candidate: str | None,
    expected_registered: list[str],
) -> None:
    options = _staged_options()
    state = TransferStageState(
        target_exists=True,
        source_columns=["id"],
        stage_column_types={"id": "BIGINT"},
        internal_columns=resolve_internal_columns(["id"], "gp"),
    )
    call_count = 0

    def create(*_args: Any, **kwargs: Any) -> str:
        nonlocal call_count
        callback = kwargs["on_stage_candidate"]
        if call_count == 0:
            call_count += 1
            callback("stage.worker_0")
            return "stage.worker_0"
        if ambiguous_candidate is not None:
            callback(ambiguous_candidate)
        raise OSError("worker stage create failed")

    monkeypatch.setattr(staged_attempt, "create_stage_table", create)

    with pytest.raises(OSError, match="worker stage create failed"):
        staged_attempt._create_worker_stages(
            options,
            {"connection": object()},
            state,
            worker_count=2,
        )

    assert state.stage_table == "stage.worker_0"
    assert state.stage_tables == expected_registered
    assert state.stage_table_created is True

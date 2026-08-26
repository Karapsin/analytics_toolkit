from __future__ import annotations

from tests.sql._support.row_batches import (
    Any,
    FakeTransferConnection,
    SimpleNamespace,
    attempt_module,
    keyed_module,
    load_sql_table_module,
    make_progress_options,
    models_module,
    pytest,
    retry_module,
    transfer_api_module,
)


def test_callable_commit_and_failed_keyed_future_cancels_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits: list[str] = []
    attempt_module._commit_if_supported(SimpleNamespace(commit=lambda: commits.append("commit")))
    attempt_module._commit_if_supported(SimpleNamespace(commit="not callable"))
    assert commits == ["commit"]

    class Future:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error
            self.cancelled = False

        def exception(self) -> Exception | None:
            return self.error

        def result(self) -> int:
            return 1

        def cancel(self) -> None:
            self.cancelled = True

    failed = Future(RuntimeError("worker failed"))
    pending = Future()

    class Executor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def submit(self, *_args: Any, **_kwargs: Any) -> Future:
            return failed if _kwargs["worker_stage_state"].worker_index == 0 else pending

    monkeypatch.setattr(attempt_module, "ThreadPoolExecutor", Executor)
    monkeypatch.setattr(attempt_module, "wait", lambda _pending, **_k: ({failed}, {pending}))
    workers = [
        keyed_module.WorkerStageState(
            worker_index=i,
            stage_state=models_module.TransferStageState(target_exists=False),
            transfer_slices=[],
        )
        for i in range(2)
    ]
    with pytest.raises(RuntimeError, match="worker failed"):
        attempt_module.load_keyed_stage_slices(
            options=make_progress_options(),
            worker_stage_states=workers,
            read_retry_cnt=1,
            insert_retry_cnt=1,
        )
    assert pending.cancelled is True


def test_gp_insert_rows_retry_replaces_closed_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_ref = {"connection": FakeTransferConnection("target-0")}
    insert_connections: list[str] = []
    replaced_connections: list[tuple[str, str]] = []
    success_calls: list[tuple[float, int]] = []

    def fake_insert_rows_backend(
        _backend: str,
        connection: FakeTransferConnection,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        insert_connections.append(connection.name)
        if len(insert_connections) == 1:
            raise RuntimeError("connection already closed")

    def fake_replace_connection(
        connection_key: str,
        connection_ref: dict[str, Any],
    ) -> None:
        old_connection = connection_ref["connection"]
        replaced_connections.append((connection_key, old_connection.name))
        old_connection.close()
        connection_ref["connection"] = FakeTransferConnection("target-1")

    monkeypatch.setattr(
        load_sql_table_module,
        "_insert_rows_backend",
        fake_insert_rows_backend,
    )

    rows = load_sql_table_module.insert_rows_batch(
        "gp",
        connection_ref,
        "stage_table",
        ["id"],
        [(1,)],
        retry_fn=retry_module.run_with_retry,
        retry_cnt=2,
        timeout_increment=0,
        connection_key="target_alias",
        rollback_fn=retry_module.rollback_quietly,
        replace_connection_fn=fake_replace_connection,
        on_success=lambda duration, inserted_rows: success_calls.append((duration, inserted_rows)),
    )

    assert rows == 1
    assert insert_connections == ["target-0", "target-1"]
    assert replaced_connections == [("target_alias", "target-0")]
    assert success_calls and success_calls[0][1] == 1
    assert connection_ref["connection"].name == "target-1"


def test_unkeyed_source_staged_full_retry_uses_safe_exception_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = make_progress_options(
        source_transfer_staging_schema="source_stage",
        replace_target_table=True,
        retry_cnt=1,
        full_retry_cnt=1,
        full_timeout_increment=0,
    )
    retry_options: list[dict[str, Any]] = []
    monkeypatch.setattr(transfer_api_module, "build_transfer_options", lambda **_k: options)
    monkeypatch.setattr(transfer_api_module, "run_transfer_attempt", lambda **_k: 3)

    def retry(**kwargs: Any) -> int:
        retry_options.append(kwargs)
        return kwargs["operation"](1)

    monkeypatch.setattr(transfer_api_module, "run_retrying_operation", retry)

    assert transfer_api_module.transfer_table("source", "target") == 3
    assert retry_options[0]["safe_exception_logging"] is True

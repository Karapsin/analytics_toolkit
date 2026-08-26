from __future__ import annotations

from tests.sql._support.lifecycle import (
    Any,
    LifecycleAdapter,
    SimpleNamespace,
    maintenance,
    pytest,
)


@pytest.mark.parametrize("vacuum_error", [None, RuntimeError("vacuum failed")])
def test_gp_vacuum_always_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
    vacuum_error: Exception | None,
) -> None:
    closed: list[bool] = []
    connection = SimpleNamespace(close=lambda: closed.append(True))
    adapter = LifecycleAdapter()

    def vacuum_table(*args: Any, **kwargs: Any) -> None:
        adapter._record("vacuum_table", *args, **kwargs)
        if vacuum_error is not None:
            raise vacuum_error

    adapter.vacuum_table = vacuum_table
    monkeypatch.setattr(
        maintenance,
        "get_connection_config",
        lambda _key: SimpleNamespace(backend="gp", connection_key="gp_alias"),
    )
    monkeypatch.setattr(maintenance, "get_sql_connection", lambda _key: connection)
    monkeypatch.setattr(maintenance, "get_backend_adapter", lambda _backend: adapter)
    monkeypatch.setattr(maintenance, "time_print", lambda *_args, **_kwargs: None)

    if vacuum_error is None:
        maintenance.gp_vacuum(
            "gp_alias",
            "schema.table",
            analyze=True,
            full=True,
            verbose=False,
        )
    else:
        with pytest.raises(RuntimeError, match="vacuum failed"):
            maintenance.gp_vacuum("gp_alias", "schema.table")

    assert closed == [True]
    assert adapter.calls[0][0] == "vacuum_table"

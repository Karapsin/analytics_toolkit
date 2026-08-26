from __future__ import annotations

from tests.sql._support.adapters import (
    get_backend_adapter,
    importlib,
    pytest,
)


def test_backend_read_dataframe_logs_failed_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    general_module = importlib.import_module("analytics_toolkit.general")
    messages: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        general_module,
        "time_print",
        lambda message, backend=None: messages.append((message, backend)),
    )

    with pytest.raises(RuntimeError, match="read failed"):
        get_backend_adapter("gp").read_dataframe(
            object(),
            "SELECT secret FROM source",
            print_queries=False,
            print_query=lambda query, enabled: None,
            read_dbapi_query=lambda connection, query: (_ for _ in ()).throw(
                RuntimeError("read failed")
            ),
        )

    assert messages == [
        ("Reading DataFrame", "gp"),
        ("Failed SQL:\nSELECT secret FROM source", "gp"),
    ]

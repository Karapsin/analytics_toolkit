from __future__ import annotations

from tests.sql._support.load_table import load_sql_table_module, pd, pytest
from tests.sql._support.retries import retry_module


def test_stage_insert_bounds_exception_names_and_suppresses_sensitive_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exception_name = "TenantConnectionFailure" * 8
    error = type(exception_name, (ConnectionError,), {})("private connection credentials")
    inserts: list[int] = []

    def fail_insert(*_args: object, **_kwargs: object) -> None:
        inserts.append(1)
        raise error

    monkeypatch.setattr(load_sql_table_module, "_insert_batch_backend", fail_insert)

    with pytest.raises(load_sql_table_module.AmbiguousTableLoadError) as caught:
        load_sql_table_module.insert_table_batch(
            "ch",
            {"connection": object()},
            "sandbox.stage",
            pd.DataFrame({"id": [1]}),
            retry_fn=retry_module.run_with_retry,
            retry_cnt=1,
            timeout_increment=0,
            safe_exception_logging=True,
        )

    assert caught.value.__cause__ is error
    assert inserts == [1]
    output = capsys.readouterr().out
    assert exception_name[:77] + "... (details suppressed)" in output
    assert exception_name not in output
    assert "private connection credentials" not in output

from __future__ import annotations

import ssl

from analytics_toolkit.sql.execution.backend_options import backend_operation

from tests.sql._support.load_table import FakeDbapiConnection, load_sql_table_module, pd, pytest
from tests.sql._support.retries import retry_module


@pytest.mark.parametrize("insert_kind", ["dataframe", "rows"])
@pytest.mark.parametrize("failure_kind", ["lost_acknowledgement", "type_mismatch", "certificate"])
def test_guarded_insert_preserves_ambiguous_and_deterministic_error_contracts(
    monkeypatch: pytest.MonkeyPatch,
    insert_kind: str,
    failure_kind: str,
) -> None:
    class TypeMismatchError(Exception):
        code = 53

    if failure_kind == "type_mismatch":
        original_error: Exception = TypeMismatchError("incompatible column type")
    elif failure_kind == "certificate":
        original_error = ssl.SSLCertVerificationError("certificate verify failed")
    else:
        original_error = ConnectionError("lost after acceptance")
    original_error.__dict__["analytics_toolkit_sql_retry_safe"] = False
    attempts: list[int] = []
    sleeps: list[float] = []

    def fail_insert(*_args: object, **_kwargs: object) -> None:
        attempts.append(1)
        raise original_error

    monkeypatch.setattr(load_sql_table_module, "_insert_batch_backend", fail_insert)
    monkeypatch.setattr(load_sql_table_module, "_insert_rows_backend", fail_insert)
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)
    expected_type = (
        load_sql_table_module.AmbiguousTableLoadError
        if failure_kind == "lost_acknowledgement"
        else type(original_error)
    )
    connection_ref = {"connection": FakeDbapiConnection()}
    with backend_operation() as state:
        state.live_submitted = True
        state.preserve_recovery = True
        with pytest.raises(expected_type) as caught:
            if insert_kind == "dataframe":
                load_sql_table_module.insert_table_batch(
                    "ch",
                    connection_ref,
                    "sandbox.stage",
                    pd.DataFrame({"id": [1]}),
                    retry_fn=retry_module.run_with_retry,
                    retry_cnt=3,
                    timeout_increment=5,
                )
            else:
                load_sql_table_module.insert_rows_batch(
                    "ch",
                    connection_ref,
                    "sandbox.stage",
                    ["id"],
                    [(1,)],
                    retry_fn=retry_module.run_with_retry,
                    retry_cnt=3,
                    timeout_increment=5,
                )

    assert attempts == [1]
    assert sleeps == []
    assert getattr(caught.value, "analytics_toolkit_sql_retry_safe", None) is False
    if failure_kind == "lost_acknowledgement":
        assert caught.value.__cause__ is original_error
        assert getattr(caught.value, "analytics_toolkit_sql_backend", None) == "ch"
    else:
        assert caught.value is original_error

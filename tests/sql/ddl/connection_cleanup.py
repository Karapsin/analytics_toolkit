from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.connection.errors import SqlConfigError

from tests.sql._support.connection_scope import patch_create_probe, patch_schema_inspection
from tests.sql._support.row_batches import retry_module


@pytest.mark.parametrize("probe_fails", [False, True])
def test_existing_create_probe_preserves_outcome_when_connection_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    probe_fails: bool,
) -> None:
    error = SqlConfigError("invalid table probe") if probe_fails else None
    connection, probes = patch_create_probe(monkeypatch, error=error)
    if probe_fails:
        with pytest.raises(SqlConfigError) as caught:
            sql.create_table(
                "gp", "sandbox.existing", table_schema={"id": "INTEGER"}, if_not_exists=True
            )
        assert caught.value is error
    else:
        result = sql.create_table(
            "gp",
            "sandbox.existing",
            table_schema={"id": "INTEGER"},
            if_not_exists=True,
            return_metadata=True,
        )
        assert result.metadata.statement_count == 0
    assert probes == ["sandbox.existing"]
    assert connection.close_calls == 1


@pytest.mark.parametrize("inspection_fails", [False, True])
def test_generated_ddl_preserves_schema_inspection_without_close_retries(
    monkeypatch: pytest.MonkeyPatch,
    inspection_fails: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = SqlConfigError("invalid source schema") if inspection_fails else None
    connection, opened, inspected = patch_schema_inspection(monkeypatch, error=error)
    sleeps: list[float] = []
    monkeypatch.setattr(retry_module.time, "sleep", sleeps.append)

    def generate() -> object:
        return sql.create_table(
            "gp_sandbox",
            "sandbox.generated",
            sql="SELECT id FROM source",
            source_db="gp",
            only_generate_sql=True,
            retry_cnt=3,
            timeout_increment=5,
        )

    if inspection_fails:
        with pytest.raises(SqlConfigError) as caught:
            generate()
        assert caught.value is error
    else:
        ddl = generate()
        assert isinstance(ddl, str)
        assert "CREATE TABLE sandbox.generated" in ddl
        assert '"id" INTEGER' in ddl
    assert opened == ["gp"]
    assert inspected == ["SELECT id FROM source"]
    assert sleeps == []
    assert connection.close_calls == 1
    assert "private connection close details" not in capsys.readouterr().out

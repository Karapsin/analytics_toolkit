from __future__ import annotations

from tests.sql._support.policies import (
    SqlConfigError,
    pytest,
    wait_module,
)


def test_clickhouse_routing_error_allows_empty_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wait_module,
        "_resolve_ch_cluster_name_for_wait",
        lambda _connection, cluster: cluster,
    )
    monkeypatch.setattr(wait_module, "_query_ch_expected_cluster_hosts", lambda *_a, **_k: 2)
    monkeypatch.setattr(wait_module, "_query_ch_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(
        wait_module,
        "_describe_ch_missing_routing_hosts",
        lambda *_args, **_kwargs: "",
    )

    with pytest.raises(SqlConfigError, match="visible on 1/2"):
        wait_module._validate_ch_shard_routing_cluster(
            object(),
            "db.target_shard",
            ch_cluster="core",
            shard_on_cluster="core",
            expected_column_types=None,
        )


def test_clickhouse_routing_schema_error_allows_empty_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = iter([2, 0])
    monkeypatch.setattr(
        wait_module,
        "_resolve_ch_cluster_name_for_wait",
        lambda _connection, cluster: cluster,
    )
    monkeypatch.setattr(wait_module, "_query_ch_expected_cluster_hosts", lambda *_a, **_k: 2)
    monkeypatch.setattr(wait_module, "_query_ch_count", lambda *_a, **_k: next(counts))
    monkeypatch.setattr(
        wait_module,
        "_describe_ch_cluster_schema_mismatch",
        lambda *_args, **_kwargs: "",
    )

    with pytest.raises(SqlConfigError, match="observed 0/2"):
        wait_module._validate_ch_shard_routing_cluster(
            object(),
            "db.target_shard",
            ch_cluster="core",
            shard_on_cluster="core",
            expected_column_types={"id": "Int64"},
        )

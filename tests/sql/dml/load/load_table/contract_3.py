from __future__ import annotations

from tests.sql._support.load_table import (
    SimpleNamespace,
    ch_wait_module,
    parquet_stage_module,
    pd,
    pytest,
)


def test_wait_for_clickhouse_distributed_pair_absence_polls_cluster_tables() -> None:
    class ClusterDropClient:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.visible_rows = [
                [
                    ("host-a", "analytics", "events", "Distributed"),
                    ("host-a", "analytics", "events_shard", "ReplicatedMergeTree"),
                ],
                [],
            ]

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "FROM system.clusters" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": self.visible_rows.pop(0)},
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    client = ClusterDropClient()

    ch_wait_module._wait_for_ch_distributed_table_pair_absence(
        client,
        "analytics.events",
        ch_cluster="{cluster}",
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    cluster_table_queries = [query for query in client.queries if "system, tables" in query]
    assert len(cluster_table_queries) == 2
    assert "AND name = 'events'" in cluster_table_queries[0]
    assert "AND name = 'events_shard'" in cluster_table_queries[0]


def test_wait_for_clickhouse_distributed_pair_absence_reports_leftover_hosts() -> None:
    class StaleDropClient:
        def query(self, sql: str) -> object:
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "FROM system.clusters" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type(
                    "FakeResult",
                    (),
                    {
                        "result_rows": [
                            (
                                "host-b",
                                "analytics",
                                "events_shard",
                                "ReplicatedMergeTree",
                            )
                        ]
                    },
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    with pytest.raises(TimeoutError) as exc_info:
        ch_wait_module._wait_for_ch_distributed_table_pair_absence(
            StaleDropClient(),
            "analytics.events",
            ch_cluster="{cluster}",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )

    message = str(exc_info.value)
    assert "host-b: analytics.events_shard (ReplicatedMergeTree)" in message
    assert "ch_retry_per_host_drops=True" in message


def test_wait_for_clickhouse_distributed_pair_polls_cluster_schema() -> None:
    class ClusterSchemaClient:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.matching_counts = {
                "events": [1, 4],
                "events_shard": [4],
            }

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if sql.startswith("EXISTS TABLE "):
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "FROM system.clusters" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, columns" in sql:
                table_name = sql.split("AND table = '", 1)[1].split("'", 1)[0]
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": [(self.matching_counts[table_name].pop(0),)]},
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    client = ClusterSchemaClient()

    ch_wait_module._wait_for_ch_distributed_table_pair(
        client,
        "analytics.events",
        ch_cluster="{cluster}",
        timeout_seconds=1,
        poll_interval_seconds=0,
        expected_column_types={
            "month_date": "Date",
            "cheque_cnt_total": "Decimal(38, 5)",
        },
    )

    cluster_column_queries = [query for query in client.queries if "system, columns" in query]
    assert len(cluster_column_queries) == 3
    assert "clusterAllReplicas('core', system, columns)" in cluster_column_queries[0]
    assert "WHERE database = 'analytics'" in cluster_column_queries[0]
    assert "AND table = 'events'" in cluster_column_queries[0]
    assert (
        "name = 'month_date' AND replaceRegexpAll(type, '\\\\s+', '') = 'Date'"
        in cluster_column_queries[0]
    )
    assert (
        "name = 'cheque_cnt_total' "
        "AND replaceRegexpAll(type, '\\\\s+', '') = 'Decimal(38,5)'" in cluster_column_queries[0]
    )
    assert "AND table = 'events_shard'" in cluster_column_queries[2]


def test_wait_for_clickhouse_distributed_pair_polls_cluster_tables() -> None:
    class ClusterVisibilityClient:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.visible_counts = {
                "events": [1, 2, 3],
                "events_shard": [3],
            }

        def query(self, sql: str) -> object:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if sql.startswith("EXISTS TABLE "):
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(3,)]})()
            if "system, tables" in sql:
                table_name = sql.split("AND name = '", 1)[1].split("'", 1)[0]
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": [(self.visible_counts[table_name].pop(0),)]},
                )()
            if "system, columns" in sql:
                return type(
                    "FakeResult",
                    (),
                    {"result_rows": [(sql.count("name = ") * 3 or 3,)]},
                )()
            raise AssertionError(f"Unexpected query: {sql}")

    client = ClusterVisibilityClient()

    ch_wait_module._wait_for_ch_distributed_table_pair(
        client,
        "analytics.events",
        ch_cluster="{cluster}",
        timeout_seconds=1,
        poll_interval_seconds=0,
    )

    assert "EXISTS TABLE analytics.events" in client.queries
    assert "EXISTS TABLE analytics.events_shard" in client.queries
    cluster_table_queries = [query for query in client.queries if "system, tables" in query]
    assert len(cluster_table_queries) == 4
    assert "clusterAllReplicas('core', system, tables)" in cluster_table_queries[0]
    assert "WHERE database = 'analytics'" in cluster_table_queries[0]
    assert "AND name = 'events'" in cluster_table_queries[0]
    assert "AND name = 'events_shard'" in cluster_table_queries[3]


def test_wait_for_clickhouse_distributed_pair_reports_schema_mismatch() -> None:
    class StaleSchemaClient:
        def query(self, sql: str) -> object:
            if sql.startswith("SELECT getMacro("):
                return type("FakeResult", (), {"result_rows": [("core",)]})()
            if sql.startswith("EXISTS TABLE "):
                return type("FakeResult", (), {"result_rows": [(1,)]})()
            if "system, one" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "system, tables" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            if "GROUP BY name, type" in sql:
                return type(
                    "FakeResult",
                    (),
                    {
                        "result_rows": [
                            ("month_date", "Date", 2),
                            ("cheque_cnt_total", "UInt8", 2),
                        ]
                    },
                )()
            if "system, columns" in sql:
                return type("FakeResult", (), {"result_rows": [(2,)]})()
            raise AssertionError(f"Unexpected query: {sql}")

    with pytest.raises(TimeoutError) as exc_info:
        ch_wait_module._wait_for_ch_distributed_table_pair(
            StaleSchemaClient(),
            "analytics.events",
            ch_cluster="{cluster}",
            timeout_seconds=0,
            poll_interval_seconds=0,
            expected_column_types={
                "month_date": "Date",
                "cheque_cnt_total": "Decimal(38, 5)",
            },
        )

    message = str(exc_info.value)
    assert "schema did not match expected columns" in message
    assert "cheque_cnt_total" in message
    assert "expected Decimal(38, 5)" in message
    assert "observed UInt8 on 2 host(s)" in message


def test_write_dataframe_to_parquet_stage_uses_one_spooled_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_spooled_files = 0
    max_active_spooled_files = 0
    uploaded: list[tuple[str, object]] = []

    class FakeSpooledFile:
        _rolled = False

        def __init__(self, max_size: int) -> None:
            nonlocal active_spooled_files, max_active_spooled_files
            assert max_size == parquet_stage_module.PARQUET_STAGE_MAX_SPOOL_BYTES
            active_spooled_files += 1
            max_active_spooled_files = max(
                max_active_spooled_files,
                active_spooled_files,
            )
            self.closed = False

        def seek(self, position: int) -> None:
            assert position == 0

        def close(self) -> None:
            nonlocal active_spooled_files
            self.closed = True
            active_spooled_files -= 1

        def getvalue(self) -> bytes:
            raise AssertionError("load_df Parquet staging must not materialize bytes")

    class FakeArrowTable:
        @staticmethod
        def from_pandas(chunk: pd.DataFrame, preserve_index: bool) -> dict[str, int]:
            assert preserve_index is False
            return {"rows": len(chunk)}

    fake_pa = SimpleNamespace(Table=FakeArrowTable)

    monkeypatch.setattr(
        parquet_stage_module.tempfile,
        "SpooledTemporaryFile",
        FakeSpooledFile,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "write_arrow_table_to_parquet",
        lambda pq, arrow_table, spooled_file, row_group_size: None,
    )
    monkeypatch.setattr(
        parquet_stage_module,
        "upload_spooled_file",
        lambda fsspec_module, spooled_file, remote_uri: uploaded.append((remote_uri, spooled_file)),
    )

    rows = parquet_stage_module.write_dataframe_to_parquet_stage(
        pd.DataFrame({"id": [1, 2, 3]}),
        stage_external_location="s3://bucket/tmp/stage/",
        pa=fake_pa,
        pq=object(),
        fsspec_module=object(),
        row_group_size=2,
    )

    assert rows == 3
    assert max_active_spooled_files == 1
    assert active_spooled_files == 0
    assert [item[0] for item in uploaded] == [
        "s3://bucket/tmp/stage/part-00000.parquet",
        "s3://bucket/tmp/stage/part-00001.parquet",
    ]
    assert all(spooled_file.closed for _uri, spooled_file in uploaded)

from __future__ import annotations

import importlib.util
import json
from typing import TYPE_CHECKING

import pytest
from release_routines import sql_integration
from tests.integration import conftest as integration_conftest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("driver", "port"),
    [("http", 18123), ("native", 19000)],
)
def test_canonical_clickhouse_alias_selects_integration_driver(
    monkeypatch: pytest.MonkeyPatch,
    driver: str,
    port: int,
) -> None:
    monkeypatch.setenv("SQL_INTEGRATION_CLICKHOUSE_DRIVER", driver)

    connections = integration_conftest._integration_connections()

    assert connections["ch"]["driver"] == driver
    assert connections["ch"]["port"] == port
    assert connections["ch_source"] == connections["ch"]
    assert connections["ch_target"] == connections["ch"]
    assert connections["ch_limited"]["driver"] == "http"


@pytest.mark.parametrize(
    ("driver", "tls_port", "hostname_port"),
    [("http", 18444, 18448), ("native", 19440, 19448)],
)
def test_clickhouse_auth_aliases_select_transport_tls_frontends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    driver: str,
    tls_port: int,
    hostname_port: int,
) -> None:
    monkeypatch.setenv("SQL_INTEGRATION_PROFILE", "auth")
    monkeypatch.setenv("SQL_INTEGRATION_CERTS", str(tmp_path))
    monkeypatch.setenv("SQL_INTEGRATION_CLICKHOUSE_DRIVER", driver)

    connections = integration_conftest._integration_connections()

    assert connections["ch_tls"]["driver"] == driver
    assert connections["ch_tls"]["port"] == tls_port
    assert connections["ch_hostname_tls"]["port"] == hostname_port


def test_run_executes_every_profile_for_both_clickhouse_drivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_run_profile(**kwargs) -> int:
        calls.append((kwargs["profile"], kwargs["clickhouse_driver"]))
        return 0

    monkeypatch.setattr(sql_integration, "run_profile", fake_run_profile)
    monkeypatch.setattr(
        sql_integration,
        "_assert_transport_scenario_parity",
        lambda **_kwargs: 0,
    )

    result = sql_integration.run(
        profile="all",
        include_greenplum=True,
        clickhouse_driver="both",
    )

    assert result == 0
    assert calls == [
        (profile, driver)
        for profile in ("core", "auth", "fault", "stress")
        for driver in ("http", "native")
    ]


def test_native_profile_fails_before_compose_when_driver_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        sql_integration.importlib.util,
        "find_spec",
        lambda name: None if name == "clickhouse_driver" else real_find_spec(name),
    )
    monkeypatch.setattr(
        sql_integration,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("compose must not start"),
    )

    assert (
        sql_integration.run_profile(
            profile="core",
            include_greenplum=False,
            clickhouse_driver="native",
        )
        == 2
    )


def test_transport_scenario_parity_rejects_different_collections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sql_integration, "ARTIFACTS_DIR", tmp_path)
    for driver, scenarios in (
        ("http", [{"scenario_id": "same", "node_id": "test_same"}]),
        ("native", [{"scenario_id": "different", "node_id": "test_different"}]),
    ):
        directory = tmp_path / "core" / driver
        directory.mkdir(parents=True)
        (directory / "collected-scenarios.json").write_text(
            json.dumps(scenarios),
            encoding="utf-8",
        )

    assert sql_integration._assert_transport_scenario_parity(profile="core") == 1
    report = json.loads((tmp_path / "core" / "transport-parity.json").read_text())
    assert report["matches"] is False

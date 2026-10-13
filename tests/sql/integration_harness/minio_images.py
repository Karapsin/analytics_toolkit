from __future__ import annotations

import re
from typing import TYPE_CHECKING

from release_routines import sql_integration
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_minio_images_are_built_from_pinned_sources_with_shared_client() -> None:
    compose = (REPO_ROOT / "integration/docker-compose.yml").read_text()
    services = dict(re.findall(r"(?ms)^  ([\w-]+):\n(.*?)(?=^  [\w-]+:|\Z)", compose))
    for name in ("minio", "minio-init", "minio-client"):
        assert "    pull_policy: never\n" in services[name]
        assert "    image: minio/" not in services[name]
    client_image = "    image: analytics-toolkit-integration-mc:2025-08-13\n"
    assert client_image in services["minio-init"]
    assert client_image in services["minio-client"]
    assert "      target: server\n" in services["minio"]
    assert "      target: client\n" in services["minio-init"]
    dockerfile = (REPO_ROOT / "integration/minio/Dockerfile").read_text()
    for commit in (
        "7394ce0dd2a80935aded936b09fa12cbb3cb8096",
        "07c3a429bfed433e49018cb0f78a52145d4bedeb",
    ):
        assert f"git fetch --depth=1 origin {commit}" in dockerfile
        assert f'test "$(git rev-parse HEAD)" = {commit}' in dockerfile
    assert "/src/LICENSE /src/CREDITS /licenses/" in dockerfile
    assert "ca-certificates curl" in dockerfile


def test_image_build_failure_retains_diagnostics_and_tears_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures = []
    teardown = []

    def capture(path: Path, command: list[str]) -> int:
        captures.append(command)
        assert path.name == "build.log"
        path.write_text("cannot fetch pinned source\n")
        return 19

    monkeypatch.setattr(sql_integration, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(sql_integration, "_capture", capture)
    monkeypatch.setattr(
        sql_integration, "_run", lambda command, **kwargs: teardown.append(command) or 0
    )
    monkeypatch.setattr(sql_integration, "_assert_teardown_clean", lambda **kwargs: 0)
    assert (
        sql_integration.run_profile(
            profile="core", include_greenplum=True, clickhouse_driver="http"
        )
        == 19
    )
    assert captures[0][-3:] == ["build", "minio", "minio-init"]
    assert (tmp_path / "core/http/build.log").read_text() == "cannot fetch pinned source\n"
    assert not (tmp_path / "core/http/startup.log").exists()
    assert teardown[0][-3:] == ["down", "--volumes", "--remove-orphans"]

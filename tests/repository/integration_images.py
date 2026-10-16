"""Keep disposable S3 services independent of unavailable upstream images."""

from __future__ import annotations

import re

import pytest

from tests._support.paths import REPO_ROOT

INTEGRATION = REPO_ROOT / "integration"


@pytest.mark.parametrize(
    ("service", "target"),
    [("minio", "server"), ("minio-init", "client"), ("minio-client", "client")],
)
def test_s3_services_build_instead_of_pulling_upstream_images(
    service: str,
    target: str,
) -> None:
    compose = (INTEGRATION / "docker-compose.yml").read_text(encoding="utf-8")
    block = re.search(rf"(?ms)^  {service}:\n(.*?)(?=^  \S|\Z)", compose)
    assert block is not None
    config = block.group(1)
    assert not re.search(r"(?m)^    image:", config)
    assert re.search(r"(?m)^    build:\n      context: \./minio\n", config)
    assert re.search(rf"(?m)^      target: {target}$", config)
    assert (INTEGRATION / "minio/Dockerfile").is_file()

    # Auth/fault overlays must not restore an image pull for these services.
    overlay = (INTEGRATION / "docker-compose.auth.yml").read_text(encoding="utf-8")
    override = re.search(rf"(?ms)^  {service}:\n(.*?)(?=^  \S|\Z)", overlay)
    if override is not None:
        assert not re.search(r"(?m)^    image:", override.group(1))


def test_s3_builds_preserve_release_commits_and_runtime_commands() -> None:
    dockerfile = (INTEGRATION / "minio/Dockerfile").read_text(encoding="utf-8")
    # These commits are the signed tags used by the original Compose images.
    installs = re.findall(r"go install github\.com/minio/(\w+)@([0-9a-f]+)", dockerfile)
    assert dict(installs) == {
        "minio": "07c3a429bfed433e49018cb0f78a52145d4bedeb",
        "mc": "7394ce0dd2a80935aded936b09fa12cbb3cb8096",
    }
    bases = re.findall(r"(?m)^FROM (\S+)", dockerfile)
    assert bases
    assert all(not base.startswith(("minio/", "quay.io/minio/")) for base in bases)
    assert all(":latest" not in base for base in bases)
    assert "CGO_ENABLED=0" in dockerfile
    assert "GOTOOLCHAIN=local" in dockerfile
    # The health probe needs curl; init/cleanup need the runtime shell and mc.
    assert re.search(r"(?m)^FROM alpine:[0-9.]+ AS runtime$", dockerfile)
    assert "apk add --no-cache ca-certificates curl" in dockerfile
    for target, binary in (("server", "minio"), ("client", "mc")):
        stage = re.search(rf"(?ms)^FROM runtime AS {target}\n(.*?)(?=^FROM |\Z)", dockerfile)
        assert stage is not None
        assert f"/go/bin/{binary} /usr/local/bin/{binary}" in stage.group(1)
        assert f'ENTRYPOINT ["{binary}"]' in stage.group(1)
        assert "/usr/share/licenses" in stage.group(1)


@pytest.mark.parametrize(
    ("service", "required"),
    [
        ("minio", "command: server /data --console-address :9001"),
        ("minio", 'test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]'),
        (
            "minio-init",
            "mc alias set integration http://minio:9000 integration integration-secret",
        ),
        ("minio-init", "mc mb --ignore-existing integration/warehouse"),
        (
            "minio-client",
            "mc alias set integration http://minio:9000 integration integration-secret",
        ),
        ("minio-client", "exec tail -f /dev/null"),
    ],
)
def test_built_s3_services_keep_health_and_bucket_setup(service: str, required: str) -> None:
    compose = (INTEGRATION / "docker-compose.yml").read_text(encoding="utf-8")
    block = re.search(rf"(?ms)^  {service}:\n(.*?)(?=^  \S|\Z)", compose)
    assert block is not None
    assert required in block.group(1)

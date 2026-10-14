from __future__ import annotations

from tests._support.paths import REPO_ROOT


def test_every_upload_entrypoint_enforces_prepared_candidate() -> None:
    scripts = (
        "release_routines/pypi_release.sh",
        "release_routines/scripts/test_pypi_release.sh",
        "release_routines/scripts/real_pypi_release.sh",
    )
    for path in scripts:
        text = (REPO_ROOT / path).read_text()
        assert "python -m release_routines.lib.release_candidate guard" in text


def test_trusted_publish_checks_index_hashes_before_skipping_existing_files() -> None:
    workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text()
    assert "check-artifacts --stage testpypi" in workflow
    assert "check-artifacts --stage pypi" in workflow
    assert workflow.count("skip-existing: true") == 2
    assert "python -m release_routines.lib.artifact_smoke --output-dir dist" in workflow

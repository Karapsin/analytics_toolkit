"""Standard-library-only evidence transport, including pre-install probe failures."""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def identity(data: bytes) -> dict[str, Any]:
    return {"byte_length": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def source_binding() -> dict[str, Any]:
    paths = (
        ".github/workflows/tests.yml",
        "agent_tools/sql_explorer_svg_evidence.py",
        "agent_tools/sql_explorer_svg_underlines.py",
        "tests/agent_tools/underline_rasterization.py",
        "tests/agent_tools/_support/raster_observations.py",
        "tests/agent_tools/_support/renderer_probe.py",
        "tests/agent_tools/_support/raster_transport.py",
        "tests/agent_tools/_support/svg_fixtures.py",
        "tests/agent_tools/_support/fixtures/caret-export-provenance.json",
        "tests/agent_tools/_support/fixtures/editor-command-caret.svg.json",
        "tests/agent_tools/_support/fixtures/keyboard-multicursor.svg.json",
        "agent_tools/sql_explorer_render_diagnostics.py",
    )
    return {
        "github": {
            key: os.environ.get(key, "unavailable")
            for key in (
                "GITHUB_SHA",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_JOB",
                "GITHUB_REPOSITORY",
                "GITHUB_WORKFLOW_REF",
                "GITHUB_WORKFLOW_SHA",
                "GITHUB_EVENT_NAME",
                "RASTER_SOURCE_HEAD",
                "RASTER_PROFILE",
                "RASTER_BASE",
            )
        },
        "source_files": {path: identity((REPO_ROOT / path).read_bytes()) for path in paths},
        "task_id": "e8dd20c11f5043518c59e21e41d4074c",
        "request_id": "c91c14870a5a",
        "plan_sha256": "a55bf7b312a52313013a3dc4022f66541ebf53db1441215f687f1caddb048783",
        "plan_review_input": {
            "head": "dba63c219c7b9923b433d546e92d674406406df5",
            "base": "4a17e91b20b2683aca365f486e6217a141ef8c5b",
            "task_context_sha256": (
                "a69fb97c95a64d87d55f4553d4f9060356212be77cbccce2c972d691b8a9774e"
            ),
            "reviewed_plan_digest": (
                "9fa32e9aec7fe58a148696606f1606bdf627bf6e93d410efd05b8478f82d2e15"
            ),
            "qualification": (
                "Reviewed input identities; not the new execution head or a check pass"
            ),
        },
        "historical_diagnostic_binding": {
            "plan_sha256": "28419c6870bd29c7f26a570c3edd8b66f0bf0754ff5ea3b8f5d0214c3f9ed45e",
            "head": "dba63c219c7b9923b433d546e92d674406406df5",
            "base": "4a17e91b20b2683aca365f486e6217a141ef8c5b",
            "task_context_sha256": (
                "95e73c54cd2568db9557ea86ccf4c21eeb5ab0d1830eae46375bad53465cea3d"
            ),
        },
        "qualification": "CI reproduction only; checkpoint/producer receipts are coordinator-bound",
    }


def write_report(path: Path, report: dict[str, Any], *, max_bytes: int = MAX_REPORT_BYTES) -> bool:
    """Never publish an over-limit report as complete or truncate encoded originals."""
    data = (json.dumps(report, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    if len(data) > max_bytes:
        report = {
            "schema_version": 1,
            "complete": False,
            "missing": ["report_byte_limit"],
            "observed_report_byte_length": len(data),
            "binding": report["binding"],
        }
        data = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data) <= max_bytes and bool(report["complete"])


def bound_artifacts(output: Path) -> list[dict[str, Any]]:
    """Reserve report space and explicitly omit files exceeding existing transport caps."""
    used = MAX_REPORT_BYTES
    omitted = []
    for path in sorted(output.rglob("*")):
        if not path.is_file():
            continue
        length = path.stat().st_size
        if length > MAX_REPORT_BYTES or used + length > MAX_ARTIFACT_BYTES:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    digest.update(chunk)
            omitted.append(
                {
                    "path": str(path.relative_to(output)),
                    "byte_length": length,
                    "sha256": digest.hexdigest(),
                    "reason": "artifact_byte_limit",
                    "status": "omitted; hash alone is incomplete evidence",
                }
            )
            path.unlink()
        else:
            used += length
    return omitted

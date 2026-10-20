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
        "tests/agent_tools/underline_geometry.py",
        "tests/agent_tools/raster_assets.py",
        "tests/agent_tools/raster_fonts.py",
        "tests/agent_tools/raster_budget.py",
        "tests/agent_tools/raster_observations.py",
        "tests/agent_tools/normalized_comparisons.py",
        "tests/agent_tools/_support/raster_observations.py",
        "tests/agent_tools/_support/renderer_probe.py",
        "tests/agent_tools/_support/raster_transport.py",
        "tests/agent_tools/_support/raster_assets.py",
        "tests/agent_tools/_support/raster_fonts.py",
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
        "request_id": "e4d7b9a074ed",
        "plan_sha256": "f07026f0f0028ea1ef576e1fbc41fe78ed95300ef50a47b4ce2ddcb853d427d8",
        "plan_review_input": {
            "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
            "base": "9d65281dd78495d23cd02db4a4f14e86eb954030",
            "task_context_sha256": (
                "423cc130faa4435922d72b299104ea6a09963e982f5272a7c98371a9c929bc97"
            ),
            "reviewed_plan_digest": (
                "41b78857b13323c4ec62feb49a728b2ceb751deed9a8eab963d3b6d4318ccc27"
            ),
            "qualification": (
                "Reviewed input identities; not the new execution head or a check pass"
            ),
        },
        "historical_recovery_binding": {
            "plan_sha256": "4e2bdf2b84669676a5596762ae360347cd598349a6fc3d257ca4a1044e566221",
            "plan_review_input": {
                "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
                "base": "9d65281dd78495d23cd02db4a4f14e86eb954030",
                "task_context_sha256": (
                    "c572965e3ce2bd00c09d90003316c33dd69aec00e4b94f56f2400edf6d836e97"
                ),
                "reviewed_plan_digest": (
                    "a1110b499bcee12794f4bf00de22493cf819a9f0dc8fbc8b5570b3d3d7fb9855"
                ),
                "qualification": (
                    "Reviewed input identities; not the new execution head or a check pass"
                ),
            },
            "current_review_input": {
                "plan_sha256": "4e2bdf2b84669676a5596762ae360347cd598349a6fc3d257ca4a1044e566221",
                "reviewed_plan_digest": (
                    "a1110b499bcee12794f4bf00de22493cf819a9f0dc8fbc8b5570b3d3d7fb9855"
                ),
                "task_context_sha256": (
                    "c572965e3ce2bd00c09d90003316c33dd69aec00e4b94f56f2400edf6d836e97"
                ),
                "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
                "base": "9d65281dd78495d23cd02db4a4f14e86eb954030",
                "checkpoint_sha256": (
                    "44b9a393e930461b9ecf533b5bf91836cc18fd25939f5bbe8182afdba070025a"
                ),
                "qualification": (
                    "Reviewed inputs only; execution and final evidence bind separately"
                ),
            },
        },
        "historical_primary_binding": {
            "plan_sha256": "5533aa7e687a55f7f5a2639cb4b11c574b7d77935173059715865c333b488e8a",
            "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
            "base": "9d65281dd78495d23cd02db4a4f14e86eb954030",
            "task_context_sha256": (
                "536aca44ceedc2415896ea362aef5b2797040308ebe7059938089582a553c3e7"
            ),
            "reviewed_plan_digest": (
                "0fd25708e343d3110b5e4caad6c46174f910873af7044c9bfceb561ea310af37"
            ),
            "qualification": (
                "Predecessor reviewed input identities; not current approval or a check pass"
            ),
        },
        "historical_review_inputs": [
            {
                "request_id": "e4d7b9a074ed",
                "plan_sha256": ("50b5f512ceab1344a28a5f98ded95c0e93986b8aa80074d242b741098b0eb240"),
                "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
                "base": "4a17e91b20b2683aca365f486e6217a141ef8c5b",
                "task_context_sha256": (
                    "acf133f34d6a3211379314dec1ce16b622fd1e08379c275016673b5b87edb9c0"
                ),
                "reviewed_plan_digest": (
                    "351b9eeae315e301764aa899e79bc5ff1994620d1ec4e25f83ceed2372e1992b"
                ),
                "qualification": "Previous reviewed input; not current approval or execution",
            },
            {
                "request_id": "e4d7b9a074ed",
                "plan_sha256": ("06272f5a97d387b2feebb76ca777377d35340ccabd5ee719ab6fa4c088eca381"),
                "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
                "base": "4a17e91b20b2683aca365f486e6217a141ef8c5b",
                "task_context_sha256": (
                    "8cbfea83d9a052926346d5e4a8fd33ad41f8734f9e7e2a4a19161fef49172e56"
                ),
                "reviewed_plan_digest": (
                    "40dd2af55f7933c3424238bf37580fc8310df5458cbd39cd0833fb6506b73ddc"
                ),
                "qualification": "Previous checkpoint binding; not current approval or execution",
            },
            {
                "request_id": "e4d7b9a074ed",
                "plan_sha256": ("7e20331c0a8febee0401e50e2a6cb75ce420da4cfb4ac899d6f63f8fee703713"),
                "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
                "base": "4a17e91b20b2683aca365f486e6217a141ef8c5b",
                "task_context_sha256": (
                    "b6810bf18f76f564ba807b7e36bacabc88ef7fbd4553c035755387f5f8221c0d"
                ),
                "reviewed_plan_digest": (
                    "550d4a0fd56f5a2d7589fb6b6ce839a51a1e756c6e0f085ac34619f6bfb76884"
                ),
                "qualification": "Previous checkpoint binding; not current approval or execution",
            },
            {
                "request_id": "c91c14870a5a",
                "plan_sha256": ("a55bf7b312a52313013a3dc4022f66541ebf53db1441215f687f1caddb048783"),
                "head": "dba63c219c7b9923b433d546e92d674406406df5",
                "base": "4a17e91b20b2683aca365f486e6217a141ef8c5b",
                "task_context_sha256": (
                    "a69fb97c95a64d87d55f4553d4f9060356212be77cbccce2c972d691b8a9774e"
                ),
                "reviewed_plan_digest": (
                    "9fa32e9aec7fe58a148696606f1606bdf627bf6e93d410efd05b8478f82d2e15"
                ),
            },
        ],
        "historical_diagnostic_binding": {
            "plan_sha256": "28419c6870bd29c7f26a570c3edd8b66f0bf0754ff5ea3b8f5d0214c3f9ed45e",
            "head": "dba63c219c7b9923b433d546e92d674406406df5",
            "base": "4a17e91b20b2683aca365f486e6217a141ef8c5b",
            "task_context_sha256": (
                "95e73c54cd2568db9557ea86ccf4c21eeb5ab0d1830eae46375bad53465cea3d"
            ),
        },
        "current_review_input": {
            "plan_sha256": "f07026f0f0028ea1ef576e1fbc41fe78ed95300ef50a47b4ce2ddcb853d427d8",
            "reviewed_plan_digest": (
                "41b78857b13323c4ec62feb49a728b2ceb751deed9a8eab963d3b6d4318ccc27"
            ),
            "task_context_sha256": (
                "423cc130faa4435922d72b299104ea6a09963e982f5272a7c98371a9c929bc97"
            ),
            "head": "82fe228321f65fb3aeabe18d9a1ac089583764dd",
            "base": "9d65281dd78495d23cd02db4a4f14e86eb954030",
            "checkpoint_sha256": (
                "da403ba483177df33bd71d858495dce641e69fe9620eb9226cef4adb325b4cbb"
            ),
            "qualification": "Reviewed inputs only; execution and final evidence bind separately",
        },
        "historical_binding_qualification": (
            "historical_primary_binding retains predecessor identities "
            "without transferring approval"
        ),
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
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
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

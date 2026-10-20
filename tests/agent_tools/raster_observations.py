from __future__ import annotations

import base64
import json
import subprocess
import sys
from copy import deepcopy
from io import BytesIO
from typing import TYPE_CHECKING, Any
from zipfile import ZipFile

import pytest
from PIL import Image

from agent_tools import sql_explorer_svg_evidence as evidence
from tests._support.paths import REPO_ROOT
from tests.agent_tools import underline_rasterization as raster
from tests.agent_tools._support import raster_observations as observations
from tests.agent_tools._support import raster_transport, renderer_probe

if TYPE_CHECKING:
    from pathlib import Path


def _png(size: tuple[int, int], color: str) -> bytes:
    with Image.new("RGB", size, color) as image, BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def _fonts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "_font_context", lambda _svg: {"system_matches": []})
    monkeypatch.setattr(evidence, "_comparison_font", lambda: {"available": False, "probe": {}})
    monkeypatch.setattr(evidence, "_observed_font_files", lambda *_args: {"complete": False})
    monkeypatch.setattr(evidence, "_version", lambda _name: "unavailable")
    monkeypatch.setattr(observations, "observe_test_font", lambda _text: {"complete": False})


def test_stage_bytes_hashes_dimensions_and_single_reduction_are_not_mocked_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence, "RASTER_VIEWPORT", (8, 5))
    calls = []
    direct, intermediate = _png((8, 5), "#abcdef"), _png((16, 10), "#123456")

    def render(svg: str) -> bytes:
        calls.append(svg)
        return direct if len(calls) == 1 else intermediate

    monkeypatch.setattr(evidence, "_render_svg", render)
    budget = observations._Budget()
    record: dict[str, Any] = {}
    svg = '<svg width="8" height="5"/>'
    with observations._stages(svg, budget, record) as reduced:
        assert reduced.size == (8, 5)
        assert reduced.getpixel((0, 0)) == (18, 52, 86)
    assert calls == [svg, evidence.supersampling_svg(svg)]
    assert record["processing"]["reductions"] == 1
    assert record["processing"]["filter"] == "Image.Resampling.LANCZOS"
    for stage, data in (("direct", direct), ("intermediate", intermediate)):
        assert base64.b64decode(record[stage]["content"], validate=True) == data
        assert record[stage]["sha256"] == evidence.digest(data)
        assert record[stage]["byte_length"] == len(data)
    assert base64.b64decode(record["svg"]["content"], validate=True) == svg.encode()


def test_pixel_samples_retain_rgb_differences_runs_and_the_unmodified_predicate() -> None:
    region = (2.0, 2.0, 5.0)
    with Image.new("RGB", (20, 10), "#202020") as plain, plain.copy() as changed:
        for x in range(2, 7):
            changed.putpixel((x, 3), (226, 226, 226))
        before = changed.tobytes(), plain.tobytes()
        result = observations.pixel_observations(changed, plain, region, raster)
        shifted = observations.pixel_observations(changed, plain, (12, 2, 5), raster)
        assert result["predicate_result"] == raster._visible_underline(changed, plain, region)
        assert result["predicate_result"] is True
        assert shifted["predicate_result"] is False
        assert result["rows"][1]["qualifying_runs"] == [1, 2, 3, 4, 5]
        assert result["rows"][1]["pixels"][0]["channel_differences"] == [194, 194, 194]
        assert len(result["rows"]) == 3
        assert all(len(row["pixels"]) == 5 for row in result["rows"])
        assert (changed.tobytes(), plain.tobytes()) == before


def test_later_cases_survive_first_failure_and_unknown_probes_stay_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fonts(monkeypatch)
    captured = observations._captured_case
    seen = []

    def first_failure(scene: str, api: Any) -> dict[str, Any]:
        if scene == "editor-command-caret":
            message = "first observation failed"
            raise RuntimeError(message)
        return captured(scene, api)

    def observe(case: dict[str, Any], api: Any, budget: Any) -> dict[str, Any]:
        seen.append(case["name"])
        return {"name": case["name"], "complete": True, "variants": list(case["variants"])}

    monkeypatch.setattr(observations, "_captured_case", first_failure)
    monkeypatch.setattr(observations, "_observe_case", observe)
    result = observations.collect_report(raster)
    assert not result["complete"]
    assert [case["name"] for case in result["cases"]] == [
        "editor-command-caret",
        "keyboard-multicursor",
        "space",
        "expanded-tab",
        "wide-glyph",
        "combining-mark",
    ]
    assert len(seen) == 5
    assert "first observation failed" in result["cases"][0]["traceback"]
    assert result["versions"]["resvg-py"] == "unavailable"
    assert result["fonts"]["comparison"]["available"] is False
    assert result["cases"][-1]["variants"] == [
        "original",
        "plain",
        "normalized",
        "stroke-removed",
        "wrong-color",
        "clipped",
        "missing-glyph",
    ]


def test_retained_fixture_identities_and_mapping_precede_any_raster() -> None:
    case = observations._captured_case("editor-command-caret", raster)
    assert case["fixture"]["decoded"]["sha256"] == case["fixture"]["provenance"]["sha256"]
    assert (
        case["fixture"]["envelope"]["sha256"] == (case["fixture"]["provenance"]["envelope_sha256"])
    )
    assert case["regions"] == raster._target_regions(
        case["variants"]["original"],
        case["mapping_inputs"]["screen_cells"],
    )
    before = deepcopy(case)
    assert observations._captured_case("editor-command-caret", raster) == before


def test_renderer_failure_keeps_direct_bytes_and_continues_other_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence, "RASTER_VIEWPORT", (16, 10))
    calls = []

    def render(svg: str) -> bytes:
        calls.append(svg)
        if svg == evidence.supersampling_svg("bad"):
            message = "intermediate renderer failure"
            raise RuntimeError(message)
        return _png((32, 20) if svg.startswith("<svg") else (16, 10), "#202020")

    monkeypatch.setattr(evidence, "_render_svg", render)
    case = {
        "name": "failure",
        "regions": [(1, 1, 2)],
        "variants": {"original": "bad", "plain": "plain", "normalized": "fixed"},
    }
    before = deepcopy(case)
    result = observations._observe_case(case, raster, observations._Budget())
    assert not result["complete"]
    failed = result["variants"]["original"]
    assert failed["direct"]["content"]
    assert failed["direct"]["dimensions"] == [16, 10]
    assert "intermediate renderer failure" in failed["traceback"]
    assert result["variants"]["normalized"]["intermediate"]["dimensions"] == [32, 20]
    assert result["variants"]["normalized"]["reduced"]["dimensions"] == [16, 10]
    assert result["variants"]["normalized"]["reduced"]["content"]
    assert result["variants"]["normalized"]["observations"]
    sample = result["variants"]["normalized"]["observations"][0]
    assert sample["target"]["region"] == (1, 1, 2)
    assert sample["wrong_cell"]["region"] == (7, 1, 2)
    assert len(sample["wrong_cell"]["rows"]) == 3
    assert all(len(row["pixels"]) == 2 for row in sample["wrong_cell"]["rows"])
    assert len(calls) == 6
    assert case == before


@pytest.mark.parametrize("kind", ["svg", "png"])
def test_exact_combined_limits_never_label_omitted_bytes_complete(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    if kind == "svg":
        monkeypatch.setattr(evidence, "MAX_SVG_BYTES", 3)
    else:
        monkeypatch.setattr(observations, "MAX_DIAGNOSTIC_PNG_BYTES", 3)
    budget = observations._Budget()
    exact = budget.retain(kind, b"abc")
    assert base64.b64decode(exact["content"], validate=True) == b"abc"
    excess = budget.retain(kind, b"d")
    assert excess["status"] == "omitted"
    assert "content" not in excess
    assert budget.missing == [kind + ":combined_byte_limit"]
    assert budget.used[kind] == 4


def test_report_capacity_is_explicit_and_cannot_return_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(observations, "MAX_REPORT_BYTES", 400)
    report = {"binding": {"head": "actual"}, "complete": True, "data": "x" * 500}
    path = tmp_path / "report.json"
    assert not observations.write_report(path, report)
    retained = json.loads(path.read_bytes())
    assert retained["missing"] == ["report_byte_limit"]
    assert retained["complete"] is False
    assert retained["binding"] == report["binding"]
    assert len(path.read_bytes()) <= 400


@pytest.mark.parametrize("exit_code", [0, 1])
def test_probe_retains_original_command_streams_and_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    def run(arguments: Any, **kwargs: Any) -> Any:
        kwargs["stdout"].write(b"original stdout\x00")
        kwargs["stderr"].write(b"original stderr\n")
        return subprocess.CompletedProcess(arguments, exit_code)

    monkeypatch.setattr(subprocess, "run", run)
    result = renderer_probe.command(["disposable-python", "probe"], tmp_path, "command")
    assert result["exit_code"] == exit_code
    assert (tmp_path / result["stdout"]["path"]).read_bytes() == b"original stdout\x00"
    assert (tmp_path / result["stderr"]["path"]).read_bytes() == b"original stderr\n"


def test_probe_wheel_metadata_identity_is_downloaded_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "renderer.whl"
    metadata = b"Name: resvg-py\nVersion: 0.3.2\nRequires-Python: >=3.8\n"
    with ZipFile(path, "w") as archive:
        archive.writestr("resvg_py-0.3.2.dist-info/METADATA", metadata)
    record = renderer_probe.wheel_identity(path)
    assert record["requires_python"] == ">=3.8"
    assert record["metadata"]["content"].encode() == metadata
    assert record["metadata"]["sha256"] == evidence.digest(metadata)
    assert record["sha256"] == evidence.digest(path.read_bytes())


def test_binding_separates_reviewed_history_from_execution_and_hashes_replayed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = raster_transport.source_binding()
    for path in initial["source_files"]:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / path).read_bytes())
    monkeypatch.setattr(raster_transport, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_SHA", "actual-workflow-checkout")
    monkeypatch.setenv("RASTER_SOURCE_HEAD", "actual-candidate")
    monkeypatch.setenv("GITHUB_RUN_ID", "new-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("RASTER_PROFILE", "py39-min")
    monkeypatch.setenv("RASTER_BASE", "actual-ci-base")
    monkeypatch.delenv("GITHUB_WORKFLOW_SHA", raising=False)
    before = raster_transport.source_binding()
    assert before["github"]["GITHUB_SHA"] == "actual-workflow-checkout"
    assert before["github"]["RASTER_SOURCE_HEAD"] == "actual-candidate"
    assert before["github"]["GITHUB_RUN_ID"] == "new-run"
    assert before["github"]["GITHUB_RUN_ATTEMPT"] == "2"
    assert before["github"]["RASTER_PROFILE"] == "py39-min"
    assert before["github"]["GITHUB_WORKFLOW_SHA"] == "unavailable"
    assert before["github"]["RASTER_BASE"] == "actual-ci-base"
    _assert_review_inputs(before)
    fixture = "tests/agent_tools/_support/fixtures/editor-command-caret.svg.json"
    original = (tmp_path / fixture).read_bytes()
    assert before["source_files"][fixture] == raster_transport.identity(original)
    (tmp_path / fixture).write_bytes(original + b" ")
    after = raster_transport.source_binding()
    assert after["source_files"][fixture] == raster_transport.identity(original + b" ")
    assert after["source_files"][fixture] != before["source_files"][fixture]
    assert after["plan_review_input"] == before["plan_review_input"]
    assert after["current_review_input"] == before["current_review_input"]
    assert after["historical_primary_binding"] == before["historical_primary_binding"]
    assert after["historical_recovery_binding"] == before["historical_recovery_binding"]
    assert after["historical_review_inputs"] == before["historical_review_inputs"]
    assert after["historical_diagnostic_binding"] == before["historical_diagnostic_binding"]
    assert after["github"] == before["github"]


def _assert_review_inputs(before: dict[str, Any]) -> None:
    assert before["plan_sha256"] == (
        "f07026f0f0028ea1ef576e1fbc41fe78ed95300ef50a47b4ce2ddcb853d427d8"
    )
    reviewed = before["current_review_input"]
    assert reviewed["plan_sha256"] == before["plan_sha256"]
    assert reviewed["task_context_sha256"] == (
        "423cc130faa4435922d72b299104ea6a09963e982f5272a7c98371a9c929bc97"
    )
    assert reviewed["reviewed_plan_digest"] == (
        "41b78857b13323c4ec62feb49a728b2ceb751deed9a8eab963d3b6d4318ccc27"
    )
    assert reviewed["checkpoint_sha256"] == (
        "da403ba483177df33bd71d858495dce641e69fe9620eb9226cef4adb325b4cbb"
    )
    assert reviewed["head"] == before["plan_review_input"]["head"]
    assert reviewed["base"] == before["plan_review_input"]["base"]
    assert reviewed["task_context_sha256"] == before["plan_review_input"]["task_context_sha256"]
    assert reviewed["reviewed_plan_digest"] == before["plan_review_input"]["reviewed_plan_digest"]
    _assert_historical_review_inputs({**before, **before["historical_recovery_binding"]})


def _assert_historical_review_inputs(before: dict[str, Any]) -> None:
    assert before["plan_sha256"] == (
        "4e2bdf2b84669676a5596762ae360347cd598349a6fc3d257ca4a1044e566221"
    )
    assert before["historical_primary_binding"]["plan_sha256"] == (
        "5533aa7e687a55f7f5a2639cb4b11c574b7d77935173059715865c333b488e8a"
    )
    assert before["request_id"] == "e4d7b9a074ed"
    assert before["plan_review_input"]["head"] == "82fe228321f65fb3aeabe18d9a1ac089583764dd"
    assert before["plan_review_input"]["base"] == "9d65281dd78495d23cd02db4a4f14e86eb954030"
    assert before["plan_review_input"]["base"] != before["github"]["RASTER_BASE"]
    assert before["historical_primary_binding"]["task_context_sha256"] == (
        "536aca44ceedc2415896ea362aef5b2797040308ebe7059938089582a553c3e7"
    )
    assert before["historical_primary_binding"]["reviewed_plan_digest"] == (
        "0fd25708e343d3110b5e4caad6c46174f910873af7044c9bfceb561ea310af37"
    )
    assert before["plan_review_input"]["task_context_sha256"] == (
        "c572965e3ce2bd00c09d90003316c33dd69aec00e4b94f56f2400edf6d836e97"
    )
    assert before["plan_review_input"]["reviewed_plan_digest"] == (
        "a1110b499bcee12794f4bf00de22493cf819a9f0dc8fbc8b5570b3d3d7fb9855"
    )
    assert before["current_review_input"]["plan_sha256"] == (
        "4e2bdf2b84669676a5596762ae360347cd598349a6fc3d257ca4a1044e566221"
    )
    assert before["current_review_input"]["task_context_sha256"] == (
        "c572965e3ce2bd00c09d90003316c33dd69aec00e4b94f56f2400edf6d836e97"
    )
    assert before["current_review_input"]["base"] == before["plan_review_input"]["base"]
    published, saved, previous, earlier = before["historical_review_inputs"]
    assert published["request_id"] == "e4d7b9a074ed"
    assert published["plan_sha256"] == (
        "50b5f512ceab1344a28a5f98ded95c0e93986b8aa80074d242b741098b0eb240"
    )
    assert published["head"] == before["plan_review_input"]["head"]
    assert published["base"] == "4a17e91b20b2683aca365f486e6217a141ef8c5b"
    assert published["base"] != before["plan_review_input"]["base"]
    assert published["task_context_sha256"] == (
        "acf133f34d6a3211379314dec1ce16b622fd1e08379c275016673b5b87edb9c0"
    )
    assert published["reviewed_plan_digest"] == (
        "351b9eeae315e301764aa899e79bc5ff1994620d1ec4e25f83ceed2372e1992b"
    )
    assert saved["request_id"] == "e4d7b9a074ed"
    assert saved["plan_sha256"] == (
        "06272f5a97d387b2feebb76ca777377d35340ccabd5ee719ab6fa4c088eca381"
    )
    assert saved["head"] == before["plan_review_input"]["head"]
    assert saved["base"] == published["base"]
    assert saved["task_context_sha256"] == (
        "8cbfea83d9a052926346d5e4a8fd33ad41f8734f9e7e2a4a19161fef49172e56"
    )
    assert saved["reviewed_plan_digest"] == (
        "40dd2af55f7933c3424238bf37580fc8310df5458cbd39cd0833fb6506b73ddc"
    )
    assert previous["request_id"] == "e4d7b9a074ed"
    assert previous["plan_sha256"] == (
        "7e20331c0a8febee0401e50e2a6cb75ce420da4cfb4ac899d6f63f8fee703713"
    )
    assert previous["task_context_sha256"] == (
        "b6810bf18f76f564ba807b7e36bacabc88ef7fbd4553c035755387f5f8221c0d"
    )
    assert previous["reviewed_plan_digest"] == (
        "550d4a0fd56f5a2d7589fb6b6ce839a51a1e756c6e0f085ac34619f6bfb76884"
    )
    assert earlier["request_id"] == "c91c14870a5a"
    assert earlier["plan_sha256"] == (
        "a55bf7b312a52313013a3dc4022f66541ebf53db1441215f687f1caddb048783"
    )
    assert before["plan_review_input"]["head"] != before["github"]["RASTER_SOURCE_HEAD"]
    assert before["historical_diagnostic_binding"]["plan_sha256"] != before["plan_sha256"]


def test_probe_bootstrap_needs_no_uninstalled_raster_dependencies() -> None:
    source = (
        "import sys\n"
        "class BlockRuntime:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] in {'PIL', 'rich', 'textual', 'resvg_py'}:\n"
        "            raise ImportError(fullname)\n"
        "sys.meta_path.insert(0, BlockRuntime())\n"
        "import tests.agent_tools._support.renderer_probe\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_command_capacity_and_timeout_preserve_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(arguments: Any, **kwargs: Any) -> Any:
        kwargs["stdout"].write(b"partial")
        raise subprocess.TimeoutExpired(arguments, 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(renderer_probe, "MAX_COMMAND_BYTES", 3)
    record = renderer_probe.command(["probe"], tmp_path, "timeout")
    assert record["exit_code"] is None
    assert not record["complete"]
    assert record["failure"] == "TimeoutExpired"
    assert record["stdout"]["byte_length"] == 7
    assert record["stdout"]["omission"] == "command_output_byte_limit"
    assert not (tmp_path / "timeout.stdout").exists()


def test_artifact_admission_preserves_caps_and_reports_original_omitted_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(raster_transport, "MAX_REPORT_BYTES", 4)
    monkeypatch.setattr(raster_transport, "MAX_ARTIFACT_BYTES", 9)
    output = tmp_path / "artifacts"
    output.mkdir()
    (output / "a").write_bytes(b"1234")
    (output / "b").write_bytes(b"12")
    omitted = raster_transport.bound_artifacts(output)
    assert len(omitted) == 1
    assert (output / "a").read_bytes() == b"1234"
    assert not (output / "b").exists()
    assert omitted[0]["path"] == "b"
    assert omitted[0]["byte_length"] == 2
    assert omitted[0]["sha256"] == evidence.digest(b"12")
    assert "incomplete" in omitted[0]["status"]


@pytest.mark.parametrize("profile", ["py39-latest", "py39-min"])
def test_probe_keeps_strict_failure_and_collects_remaining_environment_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    calls = []

    def command(arguments: list[str], output: Path, name: str) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "download":
            directory = output / "distribution"
            with ZipFile(directory / "renderer.whl", "w") as archive:
                archive.writestr(
                    "resvg_py-0.3.2.dist-info/METADATA",
                    b"Name: resvg-py\nVersion: 0.3.2\nRequires-Python: >=3.8\n",
                )
        return {
            "arguments": arguments,
            "complete": True,
            "exit_code": 1 if name == "strict-cases" else 0,
        }

    monkeypatch.setattr(renderer_probe.venv.EnvBuilder, "create", lambda *_args: None)
    monkeypatch.setattr(renderer_probe, "command", command)
    output = tmp_path / profile
    assert renderer_probe.probe(output, profile) == 1
    report = json.loads((output / "probe.json").read_bytes())
    assert report["complete"] is False
    assert [name for name, _arguments in calls] == [
        "pip",
        "download",
        "install",
        "api",
        "observations",
        "strict-cases",
        "pip-check",
        "freeze",
    ]
    arguments = dict(calls)
    assert arguments["download"][-1] == "resvg-py==0.3.2"
    install = arguments["install"]
    assert install[-1] == str(output.resolve() / "distribution/renderer.whl")
    assert "Pillow>=10,<13" in install
    if profile == "py39-min":
        assert install[install.index("-c") + 1] == "constraints/py39-min.txt"
        assert "pytest==8.3.5" in install
    else:
        assert "-c" not in install
        assert "pytest" in install
    assert arguments["strict-cases"][1:] == [
        "-m",
        "pytest",
        "-q",
        "tests/agent_tools/underline_rasterization.py",
    ]
    assert len({arguments[0] for _name, arguments in calls}) == 1
    assert calls[0][1][0] != sys.executable
    assert report["commands"][5]["exit_code"] == 1
    assert report["commands"][-1]["exit_code"] == 0
    assert report["distribution"]["requires_python"] == ">=3.8"

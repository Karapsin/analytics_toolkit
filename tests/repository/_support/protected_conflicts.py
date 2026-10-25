"""Real Git histories and effect traps for pre-worker conflict routing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from typing import Any


def preparation(
    publication: Any,
    monkeypatch: Any,
    *,
    scenario: tuple[str, bytes | None, str | None] = (
        ".github/agent/controller.py",
        b"dev changes\n",
        None,
    ),
    proposal: bytes | None = b"feature proposal\n",
) -> Any:
    path, dev, rename = scenario
    case = SimpleNamespace()
    (
        case.work,
        case.git,
        case.commit,
        case.make_task,
        _artifact,
        case.gh,
        case.pushed,
        case.history,
    ) = publication
    initial = b"original\n"
    if rename:
        initial = b"".join(f"{i}\n".encode() for i in range(100))
        proposal = initial.replace(b"50\n", b"feature\n")
        dev = initial.replace(b"50\n", b"dev\n")
    head, base, _ancestor = case.history(path, initial=initial, proposal=proposal, dev=dev)
    if rename:
        case.git("reset", "--hard", base)
        (case.work / rename).parent.mkdir(parents=True, exist_ok=True)
        case.git("mv", "--", path, rename)
        base = case.commit()
    case.git("reset", "--hard", head)
    case.task = case.make_task(head, base)
    case.control = case.work.parent / "trusted-control"
    case.ci = Mock(return_value=[])
    case.prompt = Mock()
    case.collector, case.publisher = Mock(), Mock()
    monkeypatch.setattr(controller, "_preparation_ci", case.ci)
    monkeypatch.setattr(controller, "_write_preparation_context", case.prompt)
    monkeypatch.setattr(controller, "collect", case.collector)
    monkeypatch.setattr(controller, "publish", case.publisher)
    return case


def assert_stopped(case: Any, controls: Any) -> None:
    case.ci.assert_not_called()
    case.prompt.assert_not_called()
    case.collector.assert_not_called()
    case.publisher.assert_not_called()
    assert not case.pushed
    assert not controls.pending()
    assert not controls.approved(case.gh.repository, 1, case.task["head"])


def discover_again(case: Any, monkeypatch: Any) -> list[dict[str, Any]]:
    gh = case.gh
    original_api = gh.api

    def api(endpoint: str, method: str = "GET", data: Any = None) -> Any:
        if endpoint.startswith("collaborators/"):
            return {"permission": "write"}
        if endpoint.startswith("compare/"):
            return {"behind_by": 1}
        return original_api(endpoint, method, data)

    monkeypatch.setattr(gh, "api", api)
    monkeypatch.setattr(
        gh,
        "pages",
        lambda endpoint, key=None: gh.comments if endpoint.startswith("issues/") else [],
    )
    monkeypatch.setattr(gh, "pulls", Mock(return_value=[gh.pr]), raising=False)
    monkeypatch.setattr(gh, "checks", Mock(return_value=[]), raising=False)
    monkeypatch.setattr(
        controller,
        "monitor",
        lambda _gh: (
            1,
            {"started": "2026-01-01T00:00:00Z", "runs": {}, "failures": {}, "candidates": {}},
        ),
    )
    monkeypatch.setattr(controller, "save_monitor", Mock())
    return controller.discover(gh, 17)["include"]

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from tests.repository.agent_lifecycle import publication_context, reference_publication
from tests.repository.github_agent import controller

if TYPE_CHECKING:
    from pathlib import Path

# Import the fixture explicitly for pytest discovery in this module.
__all__ = ["reference_publication"]

DEFECTS = [
    "missing",
    "changed",
    "non-png",
    "oversized",
    "empty",
    "symlink",
    "unreadable",
    "byte-count",
    "malformed",
    "duplicate",
]


def damaged_blob(original: Any, context: dict[str, Any], defect: str) -> Any:
    path = context["images"][0]["path"]

    def command(*args: str, **kwargs: Any) -> Any:
        if args[1] == "ls-tree":
            if defect == "missing":
                return b""
            if defect == "malformed":
                return b"invalid\0"
            if defect == "duplicate":
                return original(*args, **kwargs) * 2
            if defect == "symlink":
                return ("120000 blob " + "f" * 40 + "\t" + path + "\0").encode()
        if args[1] == "cat-file":
            if defect == "unreadable":
                raise RuntimeError("Image evidence object is unreadable.")
            if args[2] == "-s":
                if defect == "oversized":
                    return "524289"
                if defect == "empty":
                    return "0"
                if defect == "byte-count":
                    return "1"
            if args[2] == "blob":
                if defect == "changed":
                    return b"\x89PNG\r\n\x1a\nchanged image!!"
                if defect == "non-png":
                    return b"x" * len(original(*args, **kwargs))
        return original(*args, **kwargs)

    return command


@pytest.mark.parametrize("defect", DEFECTS)
@pytest.mark.parametrize(
    ("boundary", "mode"),
    [
        ("staged", "feature"),
        ("staged", "correction"),
        ("staged", "conflict"),
        ("staged", "repair"),
        ("recovered", "feature"),
    ],
)
def test_invalid_images_prevent_signing_push_and_attachments(
    tmp_path: Path,
    reference_publication: Any,
    defect: str,
    boundary: str,
    mode: str,
) -> None:
    context = publication_context("[Image #1]", "starting", "image")
    # The recovered feature branch is read after the staged-tree check.
    mode = "recovered-feature" if boundary == "recovered" else mode
    gh, task, git = reference_publication(context, mode)
    original = git.side_effect
    broken = damaged_blob(original, context, defect)
    checking_recovered = False

    def command(*args: str, **kwargs: Any) -> Any:
        nonlocal checking_recovered
        if args[1] == "ls-tree":
            checking_recovered = args[4] == "d" * 40
        if boundary == "staged" or checking_recovered:
            return broken(*args, **kwargs)
        return original(*args, **kwargs)

    git.side_effect = command
    with pytest.raises((RuntimeError, ValueError)):
        controller.publish(gh, task, tmp_path, tmp_path)
    assert not any(call.args[1] in {"commit-tree", "push"} for call in git.call_args_list)
    controller.signing_options.assert_not_called()
    gh.api.assert_not_called()
    gh.comment.assert_called_once()  # Blocker reporting remains permitted.
    if task["number"]:
        gh.label.assert_called_once_with(task["number"], acquire=False)


@pytest.mark.parametrize("defect", DEFECTS)
def test_signed_head_metadata_rejects_invalid_git_blobs(
    tmp_path: Path, monkeypatch: Any, defect: str
) -> None:
    context = publication_context("[Image #1]", "starting", "image")
    proto = controller.trusted_module("task_context")
    path = context["images"][0]["path"]
    png = b"\x89PNG\r\n\x1a\nsanitized image"

    def command(*args: str, **kwargs: Any) -> Any:
        if args[1] == "log":
            return "Signed feature" + proto.trailers(context, context["producer_id"])
        if args[1] == "ls-tree":
            return ("100644 blob " + "f" * 40 + "\t" + path + "\0").encode()
        if args[2] == "-t":
            return "blob"
        if args[2] == "-s":
            return str(len(png))
        assert kwargs["raw_stdout"]
        return png

    git = Mock(side_effect=damaged_blob(command, context, defect))
    monkeypatch.setattr(controller, "command", git)
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(tmp_path))
    gh = Mock()
    pr = {
        "body": proto.render(context),
        "head": {"sha": "a" * 40, "ref": "codex/image-feature"},
        "user": {"type": "User"},
    }
    with pytest.raises((RuntimeError, ValueError)):
        controller.task_metadata(gh, pr)
    gh.api.assert_not_called()  # Contents API can dereference symlinks.


def test_signed_head_metadata_accepts_approved_git_png(tmp_path: Path, monkeypatch: Any) -> None:
    context = publication_context("[Image #1]", "starting", "image")
    proto = controller.trusted_module("task_context")
    path = context["images"][0]["path"]
    png = b"\x89PNG\r\n\x1a\nsanitized image"

    def command(*args: str, **kwargs: Any) -> Any:
        if args[1] == "log":
            return "Signed feature" + proto.trailers(context, context["producer_id"])
        if args[1] == "ls-tree":
            return ("100644 blob " + "f" * 40 + "\t" + path + "\0").encode()
        if args[2] == "-t":
            return "blob"
        if args[2] == "-s":
            return str(len(png))
        return png

    monkeypatch.setattr(controller, "command", command)
    monkeypatch.setenv("AGENT_CONTROL_DIR", str(tmp_path))
    gh = Mock()
    pr = {
        "body": proto.render(context),
        "head": {"sha": "a" * 40, "ref": "codex/image-feature"},
        "user": {"type": "User"},
    }
    assert controller.task_metadata(gh, pr)["user_task"] == context
    gh.api.assert_not_called()

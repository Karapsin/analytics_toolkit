from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.repository._support.capture_profile import (
    HEAD,
    SHA,
    D,
    R,
    T,
    private_root,
    source_reference,
)


def test_immutable_descriptor_safe_publication_and_exact_existing_retry(tmp_path):
    root = private_root(tmp_path)
    with D.Store(root) as store:
        store.put(("own", "nested", "proof.json"), b"original")
        store.put(("own", "nested", "proof.json"), b"original")
        assert store.read(("own", "nested", "proof.json")) == b"original"
        with pytest.raises(D.Pending):
            store.put(("own", "nested", "proof.json"), b"changed")
        for parts in (("..", "proof"), ("absolute/path",), (".",)):
            with pytest.raises(ValueError, match=r"ledger|trusted"):
                store.put(parts, b"unsafe")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "directory"])
def test_hostile_private_members_never_become_receipts(tmp_path, kind):
    root = private_root(tmp_path)
    member = root / "proof.json"
    if kind == "directory":
        member.mkdir()
    else:
        member.write_bytes(b"evidence")
        member.chmod(0o600)
        if kind == "symlink":
            member.rename(root / "target")
            member.symlink_to(root / "target")
        elif kind == "hardlink":
            os.link(member, root / "other")
        else:
            member.chmod(0o644)
    with D.Store(root) as store, pytest.raises((OSError, D.Pending)):
        store.read(("proof.json",))


def test_parent_symlink_and_loose_root_rejected(tmp_path):
    root = private_root(tmp_path)
    root.chmod(0o755)
    with pytest.raises(D.Pending):
        D.Store(root)
    root.chmod(0o700)
    (root / "redirect").symlink_to(tmp_path, target_is_directory=True)
    with D.Store(root) as store, pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        store.put(("redirect", "unsafe"), b"never")
    assert not (tmp_path / "unsafe").exists()


def deployment_intent():
    return {
        "schema_version": 1,
        "kind": "capture-source-deployment-intent-v1",
        "repository": T.REPOSITORY,
        "operation_id": "d" * 32,
        "expected_main": HEAD,
        **dict.fromkeys(
            (
                "source_set_sha256",
                "composed_origin_proof_sha256",
                "native_core_proof_sha256",
                "whole_controller_admission_proof_sha256",
                "operator_compatibility_proof_sha256",
                "authority_snapshot_sha256",
                "independent_operation_review_sha256",
                "helper_sha256",
                "payload_sha256",
            ),
            SHA,
        ),
    }


class PinHTTP:
    def __init__(self, *, conflict=False, uncertain=False, move=False):
        self.posts = []
        self.created = conflict
        self.conflict, self.uncertain, self.move = conflict, uncertain, move

    def request(self, path, method="GET", payload=None):
        status = 200
        value = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": HEAD}}
        if path.startswith("git/ref/tags/"):
            status = 200 if self.created else 404
            value = source_reference()
            if self.conflict:
                value["object"]["sha"] = "f" * 40
                value["object"]["url"] = (
                    "https://api.github.com/repos/" + T.REPOSITORY + "/git/commits/" + "f" * 40
                )
        elif method == "POST":
            self.posts.append(payload)
            self.created = True
            status = 500 if self.uncertain else 201
            value = source_reference()
        elif self.move and self.posts:
            value["object"]["sha"] = "f" * 40
        raw = D.canonical(value)
        facts = {
            "headers_complete": True,
            "body_complete": True,
            "captured_body_bytes": len(raw),
            "observed_body_bytes": len(raw),
            "declared_body_bytes": len(raw),
            "body_sha256": D.digest(raw),
            "transport_error": "none",
            "safe_headers": dict.fromkeys(
                (
                    "request_id",
                    "retry_after_seconds",
                    "rate_limit_remaining",
                    "rate_limit_reset_epoch",
                    "content_type",
                )
            ),
        }
        return status, raw, facts


@pytest.mark.parametrize("uncertain", [False, True])
def test_actual_create_only_primitive_recovers_exact_ref_without_second_post(tmp_path, uncertain):
    http = PinHTTP(uncertain=uncertain)
    with D.Store(private_root(tmp_path)) as store:
        result = R._create_pin_metadata(store, http, deployment_intent())
        assert result["commit"] == HEAD
        again = R._create_pin_metadata(store, http, deployment_intent())
        assert again["created"] is False
    assert len(http.posts) == 1
    assert http.posts[0] == {"ref": "refs/tags/agent-visual-source-v1-" + HEAD, "sha": HEAD}


@pytest.mark.parametrize("fault", ["conflict", "move"])
def test_pin_conflict_or_main_move_never_repairs_existing_identity(tmp_path, fault):
    http = PinHTTP(**{fault: True})
    with D.Store(private_root(tmp_path)) as store, pytest.raises(D.Pending):
        R._create_pin_metadata(store, http, deployment_intent())
    assert len(http.posts) == (0 if fault == "conflict" else 1)
    assert http.created  # No tag deletion/retarget rollback.


def test_fixed_sibling_import_rejects_arbitrary_candidate_path():
    with pytest.raises(ValueError, match=r"ledger|trusted"):
        D.sibling("../../candidate")
    assert Path(D.sibling("capture_transport").__file__).name == "capture_transport.py"

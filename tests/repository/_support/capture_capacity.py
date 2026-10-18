"""Real accounting inputs and bounded synthetic responses for capacity faults."""

from __future__ import annotations

import io

from tests.repository._support.capture_profile import HEAD, NONCE, SHA, D, T


def source_post(journal):
    binding = {
        "purpose": "source-deployment",
        "operation_id": "d" * 32,
        "deployment_intent_sha256": SHA,
        "source_tag": "agent-visual-source-v1-" + HEAD,
        "expected_trusted_head": HEAD,
    }
    payload = {"ref": "refs/tags/" + binding["source_tag"], "sha": HEAD}
    assert len(D.canonical(payload)) == 132
    return journal.mutate(
        "source-ref-create",
        "git/refs",
        payload,
        binding,
        intent_id=NONCE,
        attempt_id="d" * 32,
    )


def maximum_headers(response):
    measured = sum(len(key) + len(value) + 4 for key, value in response.headers.items())
    response.headers["X-Pad"] = "p" * (65536 - measured - len("X-Pad") - 4)
    assert sum(len(k) + len(v) + 4 for k, v in response.headers.items()) == 65536
    return response


class BoundaryHTTP(T.HTTP):
    """Charge actual earlier input, after Journal has retained its intent."""

    def __init__(self, *, remaining, **kwargs):
        super().__init__(**kwargs)
        self.target_remaining = remaining
        self.entries = []

    def request(self, path, method="GET", payload=None):
        if method == "POST":
            # No counter is assigned: each earlier input byte is really read and
            # charged to the same object; retained-copy credits are left intact.
            count = self.budget.remaining - self.target_remaining
            assert count >= 0
            with io.BytesIO(b"s" * count) as earlier_source:
                while True:
                    block = earlier_source.read(65536)
                    if not block:
                        break
                    self.budget.charge(len(block))
            self.entries.append((self.budget.used, self.budget.credit, self.budget.remaining))
        return super().request(path, method, payload)


def original_observation(quarantine, *, reference=None):
    if reference is None:
        observations = list(quarantine.glob("*/observation.json"))
        # A credit-earning earlier GET may have its own genuine empty original.
        observations = [p for p in observations if D.parse(p.read_bytes())["http_status"] == 503]
        assert len(observations) == 1
        path = observations[0]
    else:
        path = quarantine / reference["private_observation_sha256"] / "observation.json"
    return D.parse(path.read_bytes()), (path.parent / "body.bin").read_bytes()

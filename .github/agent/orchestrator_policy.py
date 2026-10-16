"""Owner-requested maintenance scope, activated only from the trusted controller."""

from __future__ import annotations

import re
from typing import Any

REQUEST = (
    'for "ALWAYS use reccomended" let\'s just spawn "plan resolver" agent on RPI '
    "which checks that recommended options and if they are aligned with user request "
    '-approves them. Also add "orchestrator" agent which is spawned every 30 minutes '
    "(no more than 1 such agent) IF at least 1 PR is open and  it basically does what "
    "you do now: finds issues in rpi agents toolkit, fixes them, etc. still no heavy "
    "checks on rpi. but it may edit atk and self host dirs. If nothng to fix - that "
    "agent ceases to exist"
)


# Authenticated eligibility answers, in order; the later clarification takes precedence.
CORRECTIONS = (
    (
        "Either repository: an open PR in analytics_toolkit or self_host should "
        "trigger the 30-minute maintenance orchestrator.\n\nForwarded by Codex from "
        "the owner’s actual chat answer, “Either repository (Recommended)”.\n\n"
        "Question: Should an open PR in either analytics_toolkit or self_host "
        "trigger the 30-minute orchestrator, or only an open analytics_toolkit PR?\n"
        "Answer: Either repository (Recommended)."
    ),
    (
        "Scope clarification from the owner: “PR in self_host is considered relevant "
        "only if it affects github agents functionality”. An open analytics_toolkit "
        "PR qualifies; an open self_host PR qualifies only when it affects GitHub "
        "agent functionality. Preserve one orchestrator globally and the 30-minute "
        "cadence.\n\nTransparently forwarded by Codex from the owner’s actual latest "
        "chat message. This refines the earlier “Either repository (Recommended)” "
        "answer. It does not grant protected exact-head policy approval. For the "
        "remaining routine implementation options, use the previously requested "
        "independent plan resolver to check alignment with this scope.\n\nContext: "
        "https://github.com/Karapsin/analytics_toolkit/pull/26#issuecomment-6190878523"
    ),
)


def context(identity: str) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        raise ValueError("Invalid maintenance task identity")
    return {
        "version": 1,
        "task_id": "orchestrator:" + identity,
        "producer_id": "orchestrator:" + identity,
        "source": "user",
        "starting_prompt": REQUEST,
        "corrections": list(CORRECTIONS),
    }

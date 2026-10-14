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


def context(identity: str) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        raise ValueError("Invalid maintenance task identity")
    return {
        "version": 1,
        "task_id": "orchestrator:" + identity,
        "producer_id": "orchestrator:" + identity,
        "source": "user",
        "starting_prompt": REQUEST,
        "corrections": [],
    }

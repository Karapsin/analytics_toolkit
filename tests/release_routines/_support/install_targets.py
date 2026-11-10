from __future__ import annotations

from packaging.requirements import Requirement


def editable_extras(target: str) -> set[str]:
    assert target == "." or target.startswith(".[")
    return set(Requirement("analytics-toolkit" + target[1:]).extras)

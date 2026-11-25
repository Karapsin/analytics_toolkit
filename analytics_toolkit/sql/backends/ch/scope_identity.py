"""Compare verified logical shards independently of replica enumeration order."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .reconfigure_facade_scope import FacadeScope, ServerIdentity


def grouped_scope_identity(
    scope: FacadeScope,
) -> dict[int, frozenset[tuple[tuple[str, int], ServerIdentity]]]:
    groups: dict[int, set[tuple[tuple[str, int], ServerIdentity]]] = {}
    for position, (member, endpoint) in enumerate(zip(scope.membership, scope.endpoints), 1):
        groups.setdefault(member[0], set()).add((endpoint.native, scope.identities[position]))
    return {shard: frozenset(peers) for shard, peers in groups.items()}

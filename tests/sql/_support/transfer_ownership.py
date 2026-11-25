"""Creation acknowledgements for orchestration tests with stubbed backend drops.

Native generation verification belongs to the stateful public ownership tests;
these callbacks verify that orchestration passes each receipt to that boundary.
"""

from __future__ import annotations

from analytics_toolkit.sql.ddl.creation_resources import CreatedTableResource
from analytics_toolkit.sql.dml.transfer.flow.stage_ownership import stage_creation_callbacks
from analytics_toolkit.sql.dml.transfer.flow.target_ownership import target_creation_callbacks


def acknowledge_stage(state, name, backend="gp"):
    callbacks = stage_creation_callbacks(state, backend)
    callbacks["on_stage_candidate"](name)
    resource = CreatedTableResource(name, backend)
    if "on_scoped_resource_created" in callbacks:
        callbacks["on_scoped_resource_created"](resource)
    callbacks["on_resource_created"](name)
    return resource


def acknowledge_target(state, name, backend="gp"):
    resource = CreatedTableResource(name, backend)
    target_creation_callbacks(state)["on_scoped_resource_created"](resource)
    return resource

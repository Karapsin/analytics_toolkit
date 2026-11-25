"""Resource protection while the transfer's target state is acquired."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.dml.transfer.runtime.models import TransferConnectionRefs

if TYPE_CHECKING:
    from collections.abc import Callable

    from analytics_toolkit.sql.dml.transfer.runtime.models import (
        TransferOptions,
        TransferStageState,
    )


def initialize_stage_state(
    options: TransferOptions,
    connection_refs: TransferConnectionRefs,
    run_target: Callable[..., Any],
    create_state: Callable[..., TransferStageState],
    close_ref: Callable[..., None],
) -> TransferStageState:
    # Target acquisition and inspection can fail before the main stage scope.
    # The already acquired source still belongs to this attempt in that case.
    try:
        return cast(
            "TransferStageState",
            run_target(
                options,
                "target_state",
                lambda target_ref: create_state(
                    options,
                    TransferConnectionRefs(source=connection_refs.source, target=target_ref),
                ),
            ),
        )
    except BaseException:
        close_ref(connection_refs.source, options.from_db_key, "source")
        connection_refs.source.pop("connection", None)
        raise

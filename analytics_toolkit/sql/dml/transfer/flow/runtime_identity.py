from __future__ import annotations

# ruff: noqa: I001, TC001, TID252

import uuid
from dataclasses import replace

from ..runtime.models import TransferOptions


def prepare_transfer_runtime(
    options: TransferOptions,
    *,
    dry_run: bool,
) -> TransferOptions:
    transfer_id = "<runtime-transfer-id>" if dry_run else uuid.uuid4().hex
    query_label = options.query_label
    if not dry_run:
        query_label = (
            f"{query_label} transfer_id={transfer_id}"
            if query_label
            else f"sql.transfer transfer_id={transfer_id}"
        )
    return replace(
        options,
        transfer_id=transfer_id,
        query_label=query_label,
    )

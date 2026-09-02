from __future__ import annotations

from enum import Enum
from importlib import import_module
from typing import MutableMapping, cast

_LEGACY_KEYPAD_ENTER_SEQUENCE = "\x1bOM"


class _ExplorerKey(str, Enum):
    KEYPAD_ENTER = "kp_enter"


def install_terminal_key_compatibility() -> None:
    """Keep keypad Enter distinct from ordinary Enter in Textual 0.73."""
    ansi_sequences = import_module("textual._ansi_sequences")
    sequence_keys = cast(
        "MutableMapping[str, object]",
        vars(ansi_sequences)["ANSI_SEQUENCES_KEYS"],
    )
    sequence_keys[_LEGACY_KEYPAD_ENTER_SEQUENCE] = (_ExplorerKey.KEYPAD_ENTER,)


__all__ = ["install_terminal_key_compatibility"]

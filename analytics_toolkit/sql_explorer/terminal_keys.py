from __future__ import annotations

from enum import Enum
from importlib import import_module
from typing import MutableMapping, cast

_LEGACY_KEYPAD_ENTER_SEQUENCE = "\x1bOM"
_CONTROL_COMPATIBLE_MODIFIERS = frozenset({"hyper", "meta", "super"})
_MODIFIED_KEY_PARTS = 2


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


def control_compatible_key(key: str) -> str:
    """Map forwarded Command/Fn-like modifiers to an Explorer Ctrl chord."""
    tokens = key.split("+")
    if len(tokens) < _MODIFIED_KEY_PARTS or tokens[-1] == "enter":
        return key
    modifiers = set(tokens[:-1])
    if not modifiers.intersection(_CONTROL_COMPATIBLE_MODIFIERS):
        return key
    modifiers.difference_update(_CONTROL_COMPATIBLE_MODIFIERS)
    modifiers.add("ctrl")
    return "+".join((*sorted(modifiers), tokens[-1]))


__all__ = ["control_compatible_key", "install_terminal_key_compatibility"]

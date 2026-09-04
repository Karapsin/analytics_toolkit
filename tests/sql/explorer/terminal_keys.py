from __future__ import annotations

from analytics_toolkit.sql_explorer.terminal_keys import (
    control_compatible_key,
    install_terminal_key_compatibility,
)
from textual import events
from textual._xterm_parser import XTermParser


def _decoded_keys(sequence: str) -> list[str]:
    return [
        event.key
        for event in XTermParser(lambda: False).feed(sequence)
        if isinstance(event, events.Key)
    ]


def test_keypad_enter_sequences_stay_distinct_from_plain_enter() -> None:
    install_terminal_key_compatibility()

    assert _decoded_keys("\x1b[57414u") == ["kp_enter"]
    assert _decoded_keys("\x1bOM") == ["kp_enter"]
    assert _decoded_keys("\r") == ["enter"]


def test_macos_command_enter_sequence_uses_supported_textual_modifier() -> None:
    assert _decoded_keys("\x1b[13;9u") in (["hyper+enter"], ["super+enter"])


def test_forwarded_macos_and_fn_like_modifiers_share_ctrl_shortcuts() -> None:
    assert control_compatible_key("meta+s") == "ctrl+s"
    assert control_compatible_key("super+w") == "ctrl+w"
    assert control_compatible_key("hyper+t") == "ctrl+t"
    assert control_compatible_key("meta+shift+tab") == "ctrl+shift+tab"
    assert control_compatible_key("ctrl+tab") == "ctrl+tab"
    assert control_compatible_key("meta+enter") == "meta+enter"

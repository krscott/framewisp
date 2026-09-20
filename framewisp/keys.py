"""Supported headless input names and VNC shortcut translation."""

from string import ascii_lowercase, digits

KEYS = {
    "return": "enter",
    "tab": "tab",
    "backspace": "bsp",
    "escape": "esc",
    "delete": "delete",
    "left": "left",
    "right": "right",
    "up": "up",
    "down": "down",
    "space": "space",
} | {character: character for character in ascii_lowercase + digits}
MODIFIERS = {"ctrl", "shift", "alt"}
CLICK_BUTTONS = {"left": 1, "right": 3}
SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}


def key_commands(chord: str) -> list[str] | None:
    """Translate a supported chord to VNC commands, or reject it before input."""
    *modifiers, key = chord.lower().split("+")
    if (
        key not in KEYS
        or any(modifier not in MODIFIERS for modifier in modifiers)
        or len(set(modifiers)) != len(modifiers)
    ):
        return None
    arguments: list[str] = []
    for modifier in modifiers:
        arguments.extend(["keydown", modifier])
    symbol = KEYS[key]
    if "shift" in modifiers:
        # wayvnc adjusts modifiers to match the supplied keysym. Send the shifted
        # symbol too, or it clears Shift (and other held modifiers) for this key.
        symbol = symbol.upper() if key in ascii_lowercase else symbol
        if key in digits:
            symbol = ")!@#$%^&*("[int(key)]
        if key == "tab":
            # vncdotool has no ISO_Left_Tab name. It sends a single character's
            # ordinal as the RFB keysym, so encode that keysym directly.
            symbol = chr(0xFE20)
    arguments.extend(["key", symbol])
    for modifier in reversed(modifiers):
        arguments.extend(["keyup", modifier])
    return arguments

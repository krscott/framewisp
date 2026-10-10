"""Supported headless input names and VNC shortcut translation."""

from string import ascii_lowercase, digits

PUNCTUATION = {
    "comma": ",",
    "period": ".",
    "slash": "/",
    "minus": "-",
    "equal": "=",
    "semicolon": ";",
    "apostrophe": "'",
    "bracketleft": "[",
    "bracketright": "]",
    "backslash": "\\",
    "grave": "`",
    "exclam": "!",
    "quotedbl": '"',
    "numbersign": "#",
    "dollar": "$",
    "percent": "%",
    "ampersand": "&",
    "parenleft": "(",
    "parenright": ")",
    "asterisk": "*",
    "plus": "+",
    "colon": ":",
    "less": "<",
    "greater": ">",
    "question": "?",
    "at": "@",
    "asciicircum": "^",
    "underscore": "_",
    "braceleft": "{",
    "bar": "|",
    "braceright": "}",
    "asciitilde": "~",
}
# Symbols on the same US-layout physical key with Shift held.
SHIFTED_KEYS = dict(zip("1234567890-=[]\\;'`,./", '!@#$%^&*()_+{}|:"~<>?'))

KEYS = (
    {
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
        "home": "home",
        "end": "end",
        "page_up": "pgup",
        "page_down": "pgdn",
    }
    | {character: character for character in ascii_lowercase + digits}
    | {f"f{number}": f"f{number}" for number in range(1, 13)}
    | PUNCTUATION
    | {character: character for character in PUNCTUATION.values()}
)
MODIFIERS = {"ctrl", "shift", "alt"}
CLICK_BUTTONS = {"left": 1, "right": 3}
SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}


def parse_chord(chord: str) -> tuple[list[str], str] | None:
    """Validate a chord and return modifiers and the normalized VNC symbol."""
    # A literal plus is either the whole chord or follows a '+' separator.
    if chord == "+" or chord.endswith("++"):
        chord = chord[:-1] + "plus"
    *modifiers, key = chord.lower().split("+")
    if (
        key not in KEYS
        or any(modifier not in MODIFIERS for modifier in modifiers)
        or len(set(modifiers)) != len(modifiers)
    ):
        return None
    return modifiers, KEYS[key]


def key_commands(chord: str) -> list[str] | None:
    """Translate a supported chord to VNC commands, or reject it before input."""
    parsed = parse_chord(chord)
    if parsed is None:
        return None
    modifiers, symbol = parsed
    if symbol in SHIFTED_KEYS.values() and "shift" not in modifiers:
        modifiers.append("shift")
    arguments: list[str] = []
    for modifier in modifiers:
        arguments.extend(["keydown", modifier])
    if "shift" in modifiers:
        # wayvnc adjusts modifiers to match the supplied keysym. Send the shifted
        # symbol too, or it clears Shift (and other held modifiers) for this key.
        symbol = symbol.upper() if symbol in ascii_lowercase else symbol
        symbol = SHIFTED_KEYS.get(symbol, symbol)
        if symbol == "tab":
            # vncdotool has no ISO_Left_Tab name. It sends a single character's
            # ordinal as the RFB keysym, so encode that keysym directly.
            symbol = chr(0xFE20)
    arguments.extend(["key", symbol])
    for modifier in reversed(modifiers):
        arguments.extend(["keyup", modifier])
    return arguments

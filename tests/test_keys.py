import pytest

from framewisp.actions import InputAction
from framewisp.lib import key_commands


def test_shortcut_releases_modifiers_in_reverse_order() -> None:
    assert key_commands("CTRL+Shift+Z") == [
        "keydown",
        "ctrl",
        "keydown",
        "shift",
        "key",
        "Z",
        "keyup",
        "shift",
        "keyup",
        "ctrl",
    ]


@pytest.mark.parametrize(
    "name,literal,shifted",
    [
        ("comma", ",", "<"),
        ("period", ".", ">"),
        ("slash", "/", "?"),
        ("minus", "-", "_"),
        ("equal", "=", "+"),
        ("semicolon", ";", ":"),
        ("apostrophe", "'", '"'),
        ("bracketleft", "[", "{"),
        ("bracketright", "]", "}"),
        ("backslash", "\\", "|"),
        ("grave", "`", "~"),
    ],
)
def test_punctuation_aliases_and_shift(name: str, literal: str, shifted: str) -> None:
    assert (
        key_commands(f"CTRL+{name.upper()}")
        == key_commands(f"Ctrl+{literal}")
        == ["keydown", "ctrl", "key", literal, "keyup", "ctrl"]
    )
    assert key_commands(f"Ctrl+Shift+{name}") == [
        "keydown",
        "ctrl",
        "keydown",
        "shift",
        "key",
        shifted,
        "keyup",
        "shift",
        "keyup",
        "ctrl",
    ]
    assert InputAction.parse("key", {"chord": f"Ctrl+{name}"}).action == "key"


@pytest.mark.parametrize(
    "name,literal",
    [
        ("exclam", "!"),
        ("quotedbl", '"'),
        ("numbersign", "#"),
        ("dollar", "$"),
        ("percent", "%"),
        ("ampersand", "&"),
        ("parenleft", "("),
        ("parenright", ")"),
        ("asterisk", "*"),
        ("plus", "+"),
        ("colon", ":"),
        ("less", "<"),
        ("greater", ">"),
        ("question", "?"),
        ("at", "@"),
        ("asciicircum", "^"),
        ("underscore", "_"),
        ("braceleft", "{"),
        ("bar", "|"),
        ("braceright", "}"),
        ("asciitilde", "~"),
    ],
)
def test_shifted_punctuation_aliases(name: str, literal: str) -> None:
    assert (
        key_commands(name)
        == key_commands(literal)
        == ["keydown", "shift", "key", literal, "keyup", "shift"]
    )
    assert (
        key_commands(f"Ctrl+{name}")
        == key_commands(f"Ctrl+{literal}")
        == [
            "keydown",
            "ctrl",
            "keydown",
            "shift",
            "key",
            literal,
            "keyup",
            "shift",
            "keyup",
            "ctrl",
        ]
    )


@pytest.mark.parametrize(
    "name,symbol",
    [("Home", "home"), ("End", "end"), ("Page_Up", "pgup"), ("Page_Down", "pgdn")]
    + [(f"F{number}", f"f{number}") for number in range(1, 13)],
)
def test_navigation_and_function_keys(name: str, symbol: str) -> None:
    assert key_commands(f"Shift+{name}") == [
        "keydown",
        "shift",
        "key",
        symbol,
        "keyup",
        "shift",
    ]


@pytest.mark.parametrize(
    "chord",
    [
        "",
        "Ctrl+",
        "Ctrl+++",
        "++",
        "Ctrl++a",
        "Ctrl+Ctrl+comma",
        "Meta+comma",
        "Ctrl+unknown",
        "F0",
        "F13",
        "Ctrl+é",
        "Ctrl+ comma",
    ],
)
def test_invalid_chords_are_rejected_before_input(chord: str) -> None:
    assert key_commands(chord) is None
    with pytest.raises(ValueError, match="Unsupported key combination"):
        InputAction.parse("key", {"chord": chord})

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

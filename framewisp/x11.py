"""X11 text input in the private Xwayland server."""

import subprocess
import sys


def type_text(text: str, *, interval: float, env: dict[str, str]) -> int:
    # X11 clients look up keysyms after receiving key events. xdotool's temporary
    # Unicode mappings can disappear before that lookup, losing characters.
    # Keep mappings in the private server until the next text command instead.
    characters = list(dict.fromkeys(char for char in text if not char.isascii()))
    if len(characters) > 128:
        print(
            "X11 typing supports at most 128 distinct non-ASCII characters per command. "
            "Split this text into smaller type commands.",
            file=sys.stderr,
        )
        return 1
    # These codes are above the US letters, modifiers and navigation keys used
    # by framewisp. Send numeric codes so xdotool cannot remap these symbols.
    available = [
        code for code in range(120, 256) if code not in {133, 134, 203, 204, 205, 206}
    ]
    codes = dict(zip(characters, available))
    mapping = "".join(
        f"keycode {code} = U{ord(char):04X}\n" for char, code in codes.items()
    )
    result = subprocess.run(
        ["xmodmap", "-"], input=mapping, text=True, env=env, check=False, timeout=10
    )
    if result.returncode:
        return result.returncode
    # Reassert the current X focus before XTest input; otherwise GTK loses the
    # first character after pointer input in the tested Xwayland setup.
    arguments = ["xdotool", "getwindowfocus", "windowfocus", "--sync"]
    for index, character in enumerate(text):
        if index and interval:
            arguments.extend(["sleep", str(interval)])
        key = str(codes[character]) if character in codes else f"0x{ord(character):02x}"
        arguments.extend(["key", "--delay", "0", key])
    return subprocess.run(
        arguments,
        env=env,
        check=False,
        timeout=15 + max(0, len(text) - 1) * interval,
    ).returncode

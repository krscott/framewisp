"""X11 text input in the private Xwayland server."""

import json
import subprocess
import sys
from pathlib import Path


def type_text(text: str, *, interval: float, env: dict[str, str]) -> int:
    # Applications resolve queued keycodes later. Never reuse a code for a
    # different character, even across completed commands while an app is busy.
    path = Path(env["XDG_RUNTIME_DIR"]) / "x11-keymap.json"
    codes: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
    characters = [
        char for char in dict.fromkeys(text) if not char.isascii() and char not in codes
    ]
    if len(codes) + len(characters) > 128:
        print(
            "X11 typing supports at most 128 distinct non-ASCII characters per session. "
            "Start a new session to use a different character set.",
            file=sys.stderr,
        )
        return 1
    # Upper US keycodes, excluding the modifier aliases. Numeric xdotool codes
    # avoid its temporary Unicode remappings, which lose queued characters.
    available = [
        code for code in range(120, 256) if code not in {133, 134, 203, 204, 205, 206}
    ]
    added = dict(zip(characters, available[len(codes) :]))
    if added:
        mapping = "".join(
            f"keycode {code} = U{ord(char):04X}\n" for char, code in added.items()
        )
        result = subprocess.run(
            ["xmodmap", "-"], input=mapping, text=True, env=env, check=False, timeout=10
        )
        if result.returncode:
            return result.returncode
        codes.update(added)
        path.write_text(json.dumps(codes))
    # The first XTest event initializes Xwayland's virtual keyboard. Release an
    # already-up modifier, then allow focus to settle before typing real keys.
    arguments = ["xdotool", "keyup", "Shift_L", "sleep", "0.1"]
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

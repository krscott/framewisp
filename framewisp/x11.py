"""X11 text input in the private Xwayland server."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from framewisp.errors import SessionError, display_command


def type_text(
    session: Path,
    text: str,
    *,
    interval: float,
    env: dict[str, str],
    cancelled: Callable[[], bool] | None = None,
) -> int:
    # Applications resolve queued keycodes later. Never reuse a code for a
    # different character, even across completed commands while an app is busy.
    path = Path(env["XDG_RUNTIME_DIR"]) / "x11-keymap.json"
    codes: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
    characters = [
        char for char in dict.fromkeys(text) if not char.isascii() and char not in codes
    ]
    if len(codes) + len(characters) > 128:
        raise SessionError(
            "X11 typing supports at most 128 distinct non-ASCII characters per session. "
            "Start a new session to use a different character set.",
        )
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
        display_command(
            session,
            ["xmodmap", "-"],
            input_text=mapping,
            env=env,
            display=env["DISPLAY"],
            timeout=10,
            cancelled=cancelled,
        )
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
    try:
        return display_command(
            session,
            arguments,
            env=env,
            display=env["DISPLAY"],
            timeout=15 + max(0, len(text) - 1) * interval,
            cancelled=cancelled,
        )
    except BaseException as error:
        # XTest keys outlive xdotool if it is interrupted between press/release.
        try:
            subprocess.run(
                [
                    "xdotool",
                    "keyup",
                    "--delay",
                    "0",
                    *dict.fromkeys(
                        str(codes[c]) if c in codes else f"0x{ord(c):02x}" for c in text
                    ),
                    "Shift_L",
                    "Shift_R",
                ],
                env=env,
                check=True,
                timeout=1,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as cleanup_error:
            # Keep the original display diagnostics, but make failed release
            # fatal to the worker so subsequent input cannot inherit held keys.
            raise RuntimeError(
                f"{error}\nCould not release X11 keys: {cleanup_error}"
            ) from cleanup_error
        raise

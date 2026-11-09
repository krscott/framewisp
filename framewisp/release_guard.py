"""Release held X11 desktop input after the attach owner exits, however it exits."""

import os
import select
import subprocess
import time
from threading import Event

# Mutter's X11 backend sends portal input as XTEST events on its own X
# connection. Ending the portal session leaves held keys and buttons down, and
# XTEST state outlives the process that pressed it. The X server ignores a
# release for a key or button that the XTEST device does not hold, and XTEST
# releases never change physical devices.
# xdotool reads a digit-led token that is not a keysym name as a keycode. "9"
# names the digit, so Escape stands for keycode 9.
RELEASE = [
    "xdotool",
    "keyup",
    "--delay",
    "0",
    "Escape",
    *map(str, range(10, 256)),
    *[word for button in "123" for word in ("mouseup", button)],
]
# Releases once to prove it can, then again when the owner's pipe closes. A new
# session keeps terminal signals, including hangup, away from it.
_GUARD = (
    'release() { "$@" >/dev/null; }; release "$@" || exit 1; echo ready;'
    ' while read -r _; do :; done; release "$@"'
)
READY_TIMEOUT = 5


def start_release_guard(stop: Event) -> subprocess.Popen[bytes] | None:
    """Start the X11 guard. Wayland compositors release input themselves."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return None
    if not os.environ.get("DISPLAY"):
        raise RuntimeError(
            "An X11 desktop attachment needs DISPLAY to release held input. "
            "Run attach from a terminal on your desktop."
        )
    guard = subprocess.Popen(
        ["bash", "-c", _GUARD, "framewisp-release", *RELEASE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert guard.stdout is not None
    try:
        deadline = time.monotonic() + READY_TIMEOUT
        while not select.select([guard.stdout], [], [], 0.05)[0]:
            if stop.is_set():
                raise InterruptedError("Attach stopped before input setup.")
            if time.monotonic() >= deadline:
                raise RuntimeError("X11 input release did not start in time.")
        if guard.stdout.readline() != b"ready\n":
            raise RuntimeError(
                "Could not release X11 input with xdotool. See the error above."
            )
    except BaseException:
        guard.kill()
        guard.wait()
        raise
    finally:
        guard.stdout.close()
    return guard


def finish_release_guard(guard: subprocess.Popen[bytes]) -> None:
    """Release held input now. The guard finishes alone if this times out."""
    assert guard.stdin is not None
    guard.stdin.close()
    try:
        guard.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass

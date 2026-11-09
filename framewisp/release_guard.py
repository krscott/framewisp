"""Release held X11 desktop input after the attach owner exits, however it exits."""

import os
import select
import subprocess
import sys
import time
from threading import Event

# Mutter's X11 backend sends portal input as XTEST events on its own X
# connection, which stays open. Ending the portal session therefore leaves held
# keys and buttons down. Any X client can release them, because XTEST state
# belongs to the device. The X server ignores a release for a key or button that
# the XTEST device does not hold, and XTEST releases never change physical
# devices.
# xdotool reads a digit-led token that is not a keysym name as a keycode. "9"
# names the digit, so Escape stands for keycode 9.
RELEASE = [
    "keyup",
    "--delay",
    "0",
    "Escape",
    *map(str, range(10, 256)),
    *[word for button in "123" for word in ("mouseup", button)],
]
# Seconds between the two final releases, for presses the owner sent just
# before it died that the desktop has not yet applied.
LATE_PRESS = 0.5
# Releases once to prove it can. When the owner's pipe closes, it releases,
# waits, and releases again. A new session keeps terminal signals, including
# hangup, away from it.
_GUARD = (
    'xdotool "$@" >/dev/null || exit 1; echo ready; while read -r _; do :; done;'
    f' xdotool "$@" sleep {LATE_PRESS} "$@" >/dev/null'
)
READY_TIMEOUT = 5


def x11_desktop() -> bool:
    """Whether attach runs on X11, where the desktop leaves held input down."""
    session = os.environ.get("XDG_SESSION_TYPE")
    if session in {"x11", "wayland"}:
        return session == "x11"
    return not os.environ.get("WAYLAND_DISPLAY")


def start_release_guard(stop: Event) -> subprocess.Popen[bytes] | None:
    """Start the X11 guard. Wayland compositors release input themselves."""
    if not x11_desktop():
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
    """Release held input without waiting, so detach's SIGKILL deadline holds."""
    assert guard.stdin is not None
    guard.stdin.close()
    if guard.poll() is not None:
        # The guard died early, so nothing would release mutter's held input.
        try:
            subprocess.Popen(
                ["xdotool", *RELEASE, "sleep", str(LATE_PRESS), *RELEASE],
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            print(
                f"Could not release X11 input: {error}. Press and release "
                "Shift, Ctrl, Alt, and each mouse button to clear held input.",
                file=sys.stderr,
            )

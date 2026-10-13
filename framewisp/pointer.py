"""Read the private compositor's cursor and pace pointer approaches."""

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from framewisp.errors import SessionError

DEFAULT_MOVE_DURATION = 0.3


def cursor_position(
    socket: Path, cancelled: Callable[[], bool]
) -> tuple[int, int] | None:
    helper = os.environ.get("FRAMEWISP_CURSOR_POSITION")
    if not helper:
        raise SessionError(
            "Cursor reader is missing. Use the Nix package/development shell, "
            "or --move-duration 0 for immediate movement."
        )
    try:
        with subprocess.Popen(
            [helper, str(socket)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            deadline = time.monotonic() + 1
            try:
                while True:
                    if cancelled():
                        raise InterruptedError(
                            "Input cancelled while reading cursor position."
                        )
                    if time.monotonic() >= deadline:
                        raise SessionError(
                            "Cursor position query timed out after 1 second."
                        )
                    try:
                        output, error = process.communicate(timeout=0.02)
                        break
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if process.returncode:
            raise SessionError(f"Cannot read cursor position: {error.strip()}")
    except OSError as error:
        raise SessionError(f"Cannot start cursor reader: {error}") from None
    if output.strip() == "unavailable":
        return None
    try:
        x, y = (int(value) for value in output.split())
    except ValueError:
        raise SessionError("Cursor reader returned invalid coordinates.") from None
    # Cursor images can intersect an output while their hotspot lies outside it.
    # VNC accepts only unsigned 16-bit coordinates; do not invent a starting point.
    return (x, y) if 0 <= x <= 65535 and 0 <= y <= 65535 else None


def approach(
    start: tuple[int, int] | None,
    end: tuple[int, int],
    duration: float,
    *,
    move: Callable[[int, int], None],
    wait: Callable[[float], None],
) -> None:
    if start == end:
        return
    if start is None or duration == 0:
        move(*end)
        return
    started = time.monotonic()
    last = start
    while True:
        elapsed = time.monotonic() - started
        fraction = min(1.0, elapsed / duration)
        eased = fraction * fraction * (3 - 2 * fraction)
        point = (
            round(start[0] + (end[0] - start[0]) * eased),
            round(start[1] + (end[1] - start[1]) * eased),
        )
        if point != last or fraction == 1:
            move(*point)
            last = point
        if fraction == 1:
            return
        wait(min(1 / 60, max(0, duration - (time.monotonic() - started))))

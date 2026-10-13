"""Read the private compositor's cursor and pace pointer approaches."""

import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from framewisp.errors import SessionError
from framewisp.sway import request

DEFAULT_MOVE_DURATION = 0.3


def cursor_position(
    runtime: Path, cancelled: Callable[[], bool]
) -> tuple[int, int] | None:
    try:
        seats = request(
            runtime, 101, deadline=time.monotonic() + 1, cancelled=cancelled
        )
        if not isinstance(seats, list) or len(cast(list[object], seats)) != 1:
            raise ValueError("Expected exactly one private compositor seat")
        seat = cast(list[object], seats)[0]
        if (
            not isinstance(seat, dict)
            or cast(dict[str, object], seat).get("name") != "seat0"
        ):
            raise ValueError("Expected the private seat0")
        cursor = cast(dict[str, object], seat).get("cursor")
        if not isinstance(cursor, dict):
            raise ValueError("Sway is missing Framewisp's cursor-position IPC patch")
        coordinates = [cast(dict[str, object], cursor).get(key) for key in ("x", "y")]
        if any(
            type(value) not in {int, float}
            or (isinstance(value, float) and not math.isfinite(value))
            for value in coordinates
        ):
            raise ValueError("Invalid Sway cursor coordinates")
        x, y = cast(list[float], coordinates)
    except InterruptedError:
        raise
    except (OSError, ValueError, RecursionError) as error:
        raise SessionError(
            f"Cannot read private cursor position: {error}. "
            "Use Framewisp's Nix package/development shell, or --move-duration 0."
        ) from None
    # VNC accepts only unsigned 16-bit coordinates; do not invent a starting point.
    return (round(x), round(y)) if 0 <= x <= 65535 and 0 <= y <= 65535 else None


def approach(
    start: tuple[int, int] | None,
    end: tuple[int, int],
    duration: float,
    *,
    move: Callable[[int, int], None],
    wait: Callable[[float], None],
) -> None:
    if start == end or start is None or duration == 0:
        # VNC button events reuse the client's last sent coordinates, which can
        # differ from the compositor after an independent cursor movement.
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

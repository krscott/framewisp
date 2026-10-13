"""Pace pointer approaches from the worker's last sent position."""

import time
from collections.abc import Callable

DEFAULT_MOVE_DURATION = 0.3


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

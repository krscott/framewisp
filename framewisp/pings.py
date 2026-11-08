"""Click pings and drag hold discs drawn from logged pointer events."""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from framewisp.compact import ActivityEvent, activity_events
from framewisp.layout import VideoLayout

PING_SECONDS = 0.5
PING_STYLE = "Style: Ping,Noto Sans,20,&HFF000000,&H00000000,1,0,0,5,0,0,0"

# A circle of this radius, scaled by percentages equal to the pixel radius.
# libass centres a drawing on its bounding box only when coordinates are positive.
RING_RADIUS = 100
RING_START_RADIUS = 10
RING_END_RADIUS = 40
RING_STROKE = 4

# A completed drag adds a translucent, borderless disc to its press ring. The disc
# follows the pointer while the button is held and fades after the release.
HOLD_RADIUS = 24
HOLD_OPACITY = 0.3
# Grow at the ring's rate, so the disc fills the ring until it reaches its size.
HOLD_GROW_SECONDS = (
    PING_SECONDS
    * (HOLD_RADIUS - RING_START_RADIUS)
    / (RING_END_RADIUS - RING_START_RADIUS)
)


def ring_path() -> str:
    near, far = 45, 155  # Bezier control points for a circle of radius 100.
    edge = 2 * RING_RADIUS
    return (
        f"m {RING_RADIUS} 0 b {far} 0 {edge} {near} {edge} {RING_RADIUS} "
        f"{edge} {far} {far} {edge} {RING_RADIUS} {edge} "
        f"{near} {edge} 0 {far} 0 {RING_RADIUS} "
        f"0 {near} {near} 0 {RING_RADIUS} 0"
    )


class PingEvent(ActivityEvent):
    action: str
    x: int
    y: int


@dataclass(frozen=True)
class Ping:
    start: float
    x: int
    y: int

    @property
    def end(self) -> float:
        return self.start + PING_SECONDS


@dataclass(frozen=True)
class Hold:
    """A drag's disc, moving at constant speed from (x, y) to (x2, y2).

    `start`, `x` and `y` are at the press, or at the clip start when the press
    came earlier. The disc then appears fully grown (`grows` is false).
    """

    start: float
    x: int
    y: int
    release: float
    x2: int
    y2: int
    grows: bool

    @property
    def end(self) -> float:
        return self.release + PING_SECONDS


def pings_for_clip(log: Path, *, origin: float, stopped: float) -> list[Ping | Hold]:
    """Return a ping per click or drag press and a hold per completed drag."""
    presses: list[PingEvent] = []
    drags: dict[str, list[PingEvent]] = {}
    for event in cast(list[PingEvent], activity_events(log)):
        if event["event"] != "ping":
            continue
        if event["action"] == "drag":
            drags.setdefault(event["id"], []).append(event)
        else:
            presses.append(event)
    markers: list[Ping | Hold] = []
    for press, *release in drags.values():
        presses.append(press)
        # An interrupted drag logs its press only and keeps just its ring.
        if not release or press["time"] >= stopped or release[0]["time"] < origin:
            continue
        # The input worker moves the pointer at constant speed while holding.
        end = release[0]
        start, x, y = press["time"], press["x"], press["y"]
        if start < origin:
            fraction = (origin - start) / (end["time"] - start)
            x = round(x + (end["x"] - x) * fraction)
            y = round(y + (end["y"] - y) * fraction)
            start = origin
        markers.append(
            Hold(
                start - origin,
                x,
                y,
                end["time"] - origin,
                end["x"],
                end["y"],
                grows=start == press["time"],
            )
        )
    # Rings come after discs so they draw on top in the shared ASS layer.
    markers.extend(
        Ping(press["time"] - origin, press["x"], press["y"])
        for press in presses
        if origin <= press["time"] < stopped
    )
    return markers


def ping_text(ping: Ping | Hold, layout: VideoLayout) -> str:
    """Draw a ring or hold disc that the clip keeps inside the GUI area."""
    gui = layout.gui
    scale = gui.height / 720
    first, last = RING_START_RADIUS * scale, RING_END_RADIUS * scale
    if isinstance(ping, Hold):
        # libass moves over the whole line, fade included, when both \move
        # times are zero (ass_parse.c in libass 0.17.4).
        held = max(1, round((ping.release - ping.start) * 1000))
        fade = held + round(PING_SECONDS * 1000)
        size = HOLD_RADIUS * scale
        alpha = f"&H{round(255 * (1 - HOLD_OPACITY)):02X}&"
        grow = (
            f"\\fscx{first}\\fscy{first}"
            f"\\t(0,{round(HOLD_GROW_SECONDS * 1000)},\\fscx{size}\\fscy{size})"
            if ping.grows
            else f"\\fscx{size}\\fscy{size}"
        )
        return (
            f"{{\\move({gui.x + ping.x},{gui.y + ping.y},"
            f"{gui.x + ping.x2},{gui.y + ping.y2},0,{held}){gui.clip}\\p1"
            f"\\bord0\\1c&H0000FF&\\1a{alpha}{grow}"
            f"\\t({held},{fade},\\1a&HFF&)}}"
            f"{ring_path()}{{\\p0}}"
        )
    return (
        f"{{\\pos({gui.x + ping.x},{gui.y + ping.y}){gui.clip}\\p1"
        f"\\bord{RING_STROKE * scale}\\3c&H0000FF&\\3a&H00&\\1a&HFF&"
        f"\\fscx{first}\\fscy{first}\\t(\\fscx{last}\\fscy{last}\\3a&HFF&)}}"
        f"{ring_path()}{{\\p0}}"
    )

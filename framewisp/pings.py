"""Click pings: logged pointer events drawn as rings in completed recordings."""

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


def pings_for_clip(log: Path, *, origin: float, stopped: float) -> list[Ping]:
    return [
        Ping(event["time"] - origin, event["x"], event["y"])
        for event in cast(list[PingEvent], activity_events(log))
        if event["event"] == "ping" and origin <= event["time"] < stopped
    ]


def ping_text(ping: Ping, layout: VideoLayout) -> str:
    """Draw a growing, fading ring that the clip keeps inside the GUI area."""
    gui = layout.gui
    scale = gui.height / 720
    first, last = RING_START_RADIUS * scale, RING_END_RADIUS * scale
    return (
        f"{{\\pos({gui.x + ping.x},{gui.y + ping.y}){gui.clip}\\p1"
        f"\\bord{RING_STROKE * scale}\\3c&H0000FF&\\3a&H00&\\1a&HFF&"
        f"\\fscx{first}\\fscy{first}\\t(\\fscx{last}\\fscy{last}\\3a&HFF&)}}"
        f"{ring_path()}{{\\p0}}"
    )

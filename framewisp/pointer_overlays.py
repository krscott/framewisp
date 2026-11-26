"""Click pings and hold discs drawn from logged button presses and releases."""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from framewisp.compact import ActivityEvent, activity_events
from framewisp.layout import VideoLayout

CLICK_PING_SECONDS = 0.5
POINTER_STYLE = "Style: Pointer,Noto Sans,20,&HFF000000,&H00000000,1,0,0,5,0,0,0"

# A circle of this radius, scaled by percentages equal to the pixel radius.
# libass centres a drawing on its bounding box only when coordinates are positive.
CIRCLE_RADIUS = 100

# A click ping grows from its start radius to its end radius as it fades.
CLICK_PING_START_RADIUS = 10
CLICK_PING_END_RADIUS = 40
CLICK_PING_STROKE = 4

# A completed drag adds a translucent, borderless hold disc to its click ping.
# The hold disc follows the pointer while the button is held and fades after the release.
HOLD_RADIUS = 24
HOLD_OPACITY = 0.3
# Grow at the click ping's rate, so the hold disc fills the click ping until it
# reaches its size.
HOLD_GROW_SECONDS = (
    CLICK_PING_SECONDS
    * (HOLD_RADIUS - CLICK_PING_START_RADIUS)
    / (CLICK_PING_END_RADIUS - CLICK_PING_START_RADIUS)
)


def circle_path() -> str:
    near, far = 45, 155  # Bezier control points for a circle of radius 100.
    edge = 2 * CIRCLE_RADIUS
    return (
        f"m {CIRCLE_RADIUS} 0 b {far} 0 {edge} {near} {edge} {CIRCLE_RADIUS} "
        f"{edge} {far} {far} {edge} {CIRCLE_RADIUS} {edge} "
        f"{near} {edge} 0 {far} 0 {CIRCLE_RADIUS} "
        f"0 {near} {near} 0 {CIRCLE_RADIUS} 0"
    )


class PointerEvent(ActivityEvent):
    action: str
    x: int
    y: int


@dataclass(frozen=True)
class ClickPing:
    start: float
    x: int
    y: int

    @property
    def end(self) -> float:
        return self.start + CLICK_PING_SECONDS


@dataclass(frozen=True)
class HoldDisc:
    """A drag's hold disc, moving at constant speed from (x, y) to (x2, y2).

    `start`, `x` and `y` are at the press, or at the clip start when the press
    came earlier. The hold disc then appears fully grown (`grows` is false).
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
        return self.release + CLICK_PING_SECONDS


def pointer_overlays_for_clip(
    log: Path, *, origin: float, stopped: float
) -> list[ClickPing | HoldDisc]:
    """Return a click ping per press and a hold disc per completed drag."""
    presses: list[PointerEvent] = []
    drags: dict[str, PointerEvent] = {}
    releases: dict[str, PointerEvent] = {}
    for event in cast(list[PointerEvent], activity_events(log)):
        if event["event"] == "release":
            releases[event["id"]] = event
        elif event["event"] == "press" and event["action"] == "drag":
            drags[event["id"]] = event
        elif event["event"] == "press":
            presses.append(event)
    markers: list[ClickPing | HoldDisc] = []
    for identifier, press in drags.items():
        presses.append(press)
        end = releases.get(identifier)
        # An interrupted drag logs its press only and keeps just its click ping.
        if end is None or press["time"] >= stopped or end["time"] < origin:
            continue
        # The input worker moves the pointer at constant speed while holding.
        start, x, y = press["time"], press["x"], press["y"]
        if start < origin:
            fraction = (origin - start) / (end["time"] - start)
            x = round(x + (end["x"] - x) * fraction)
            y = round(y + (end["y"] - y) * fraction)
            start = origin
        markers.append(
            HoldDisc(
                start - origin,
                x,
                y,
                end["time"] - origin,
                end["x"],
                end["y"],
                grows=start == press["time"],
            )
        )
    # Click pings come after hold discs so they draw on top in the shared ASS layer.
    markers.extend(
        ClickPing(press["time"] - origin, press["x"], press["y"])
        for press in presses
        if origin <= press["time"] < stopped
    )
    return markers


def pointer_overlay_text(overlay: ClickPing | HoldDisc, layout: VideoLayout) -> str:
    """Draw a click ping or hold disc that the clip keeps inside the GUI area."""
    gui = layout.gui
    scale = gui.height / 720
    first, last = CLICK_PING_START_RADIUS * scale, CLICK_PING_END_RADIUS * scale
    if isinstance(overlay, HoldDisc):
        # libass moves over the whole line, fade included, when both \move
        # times are zero (ass_parse.c in libass 0.17.4).
        held = max(1, round((overlay.release - overlay.start) * 1000))
        fade = held + round(CLICK_PING_SECONDS * 1000)
        size = HOLD_RADIUS * scale
        alpha = f"&H{round(255 * (1 - HOLD_OPACITY)):02X}&"
        grow = (
            f"\\fscx{first}\\fscy{first}"
            f"\\t(0,{round(HOLD_GROW_SECONDS * 1000)},\\fscx{size}\\fscy{size})"
            if overlay.grows
            else f"\\fscx{size}\\fscy{size}"
        )
        return (
            f"{{\\move({gui.x + overlay.x},{gui.y + overlay.y},"
            f"{gui.x + overlay.x2},{gui.y + overlay.y2},0,{held}){gui.clip}\\p1"
            f"\\bord0\\1c&H0000FF&\\1a{alpha}{grow}"
            f"\\t({held},{fade},\\1a&HFF&)}}"
            f"{circle_path()}{{\\p0}}"
        )
    return (
        f"{{\\pos({gui.x + overlay.x},{gui.y + overlay.y}){gui.clip}\\p1"
        f"\\bord{CLICK_PING_STROKE * scale}\\3c&H0000FF&\\3a&H00&\\1a&HFF&"
        f"\\fscx{first}\\fscy{first}\\t(\\fscx{last}\\fscy{last}\\3a&HFF&)}}"
        f"{circle_path()}{{\\p0}}"
    )

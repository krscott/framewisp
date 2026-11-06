"""Where the GUI and console panel sit in a recording's video.

Only this module depends on the panel's position. Everything else reads the
rectangles of a `VideoLayout`, so each overlay and FFmpeg's `pad` agree for every
position.
"""

from dataclasses import dataclass
from typing import Literal, cast, get_args

ConsolePosition = Literal["left", "right", "top", "bottom"]
CONSOLE_POSITIONS: tuple[ConsolePosition, ...] = get_args(ConsolePosition)
DEFAULT_CONSOLE_POSITION: ConsolePosition = "right"
DEFAULT_CONSOLE_SIZE = 640

# libx264 refuses anything larger, which would otherwise surface only after capture.
MAX_VIDEO_DIMENSION = 16384
MIN_TEXT_COLUMNS = 20
MIN_TEXT_ROWS = 3
MIN_PANEL_WIDTH = 390
MIN_PANEL_HEIGHT = 136


def text_columns(panel_width: int) -> int:
    """Columns after the side padding and the `seconds | ` line prefix."""
    return (panel_width - 32) * 10 // 119 - 10


def text_rows(panel_height: int) -> int:
    """Rows below the panel title."""
    return (panel_height - 64) // 24


@dataclass(frozen=True)
class ConsolePanel:
    """A requested panel. Its size is checked here; the display's in `video_layout`."""

    position: ConsolePosition = DEFAULT_CONSOLE_POSITION
    size: int = DEFAULT_CONSOLE_SIZE

    def __post_init__(self) -> None:
        if self.position not in CONSOLE_POSITIONS:
            raise ValueError(
                f"console position must be one of {', '.join(CONSOLE_POSITIONS)}."
            )
        if type(self.size) is not int or self.size <= 0:
            raise ValueError("--console-size must be a positive even integer.")
        if self.size > MAX_VIDEO_DIMENSION:
            # A request can hold an integer too long to format into a message.
            raise ValueError(
                f"--console-size must be at most {MAX_VIDEO_DIMENSION}, "
                "the encoder's limit for a video dimension."
            )
        if self.size % 2:
            # Yuv420p video needs even dimensions, and `pad` rounds odd offsets down.
            raise ValueError(
                f"--console-size must be even so the video can be encoded; "
                f"use {self.size + 1} instead of {self.size}."
            )
        if self.position in ("left", "right"):
            if self.size < MIN_PANEL_WIDTH:
                raise ValueError(
                    f"--console-size {self.size} is too narrow for {MIN_TEXT_COLUMNS} text "
                    f"columns; a {self.position} panel needs at least {MIN_PANEL_WIDTH}."
                )
        elif self.size < MIN_PANEL_HEIGHT:
            raise ValueError(
                f"--console-size {self.size} is too short for {MIN_TEXT_ROWS} text "
                f"rows; a {self.position} panel needs at least {MIN_PANEL_HEIGHT}."
            )

    @staticmethod
    def parse(raw: object) -> "ConsolePanel | None":
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError(
                "console must be null or an object with position and size."
            )
        settings = cast(dict[str, object], raw)
        if settings.keys() != {"position", "size"}:
            raise ValueError("console requires only position and size.")
        return ConsolePanel(
            position=cast(ConsolePosition, settings["position"]),
            size=cast(int, settings["size"]),
        )

    def parameters(self) -> dict[str, object]:
        return {"position": self.position, "size": self.size}


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def clip(self) -> str:
        """An ASS override that confines an event to this rectangle."""
        return f"\\clip({self.x},{self.y},{self.right},{self.bottom})"


@dataclass(frozen=True)
class VideoLayout:
    width: int
    height: int
    gui: Rect
    panel: Rect | None = None


def plain_layout(display: tuple[int, int]) -> VideoLayout:
    width, height = display
    return VideoLayout(width, height, Rect(0, 0, width, height))


def video_layout(
    display: tuple[int, int], console: ConsolePanel | None
) -> VideoLayout | str:
    """Lay out a clip, or say why this display can't show the requested panel."""
    if console is None:
        return plain_layout(display)
    width, height = display
    beside = console.position in ("left", "right")
    panel_width = console.size if beside else width
    panel_height = height if beside else console.size
    if panel_height < MIN_PANEL_HEIGHT:
        return (
            f"A console panel needs a display at least {MIN_PANEL_HEIGHT} pixels tall "
            f"for {MIN_TEXT_ROWS} text rows, but this display is {width}x{height}. "
            "Use a taller display, or --console-position top or bottom."
        )
    if panel_width < MIN_PANEL_WIDTH:
        return (
            f"A console panel needs a display at least {MIN_PANEL_WIDTH} pixels wide "
            f"for {MIN_TEXT_COLUMNS} text columns, but this display is {width}x{height}. "
            "Use a wider display, or --console-position left or right."
        )
    video_width = width + panel_width if beside else width
    video_height = height if beside else height + panel_height
    if max(video_width, video_height) > MAX_VIDEO_DIMENSION:
        return (
            f"A {console.size}-pixel {console.position} console panel makes the video "
            f"{video_width}x{video_height}, over the {MAX_VIDEO_DIMENSION}-pixel limit "
            f"of the encoder. Use a smaller --console-size."
        )
    return VideoLayout(
        video_width,
        video_height,
        Rect(
            panel_width if console.position == "left" else 0,
            panel_height if console.position == "top" else 0,
            width,
            height,
        ),
        Rect(
            width if console.position == "right" else 0,
            height if console.position == "bottom" else 0,
            panel_width,
            panel_height,
        ),
    )

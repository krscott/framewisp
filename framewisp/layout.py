"""Where the GUI and console panel sit in a recording's video.

Only this module depends on the panel's position. Everything else reads the
rectangles of a `VideoLayout`, so each overlay and FFmpeg's `pad` agree for every
position.
"""

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, cast, get_args

ConsolePosition = Literal["left", "right", "top", "bottom"]
CONSOLE_POSITIONS: tuple[ConsolePosition, ...] = get_args(ConsolePosition)
DEFAULT_CONSOLE_POSITION: ConsolePosition = "right"
DEFAULT_CONSOLE_SIZE = 640
DEFAULT_CELL_SCALE = 1.0

# libx264 and FFmpeg refuse anything larger, which would otherwise surface only
# after capture. FFmpeg requires (width + 128) * (height + 128) below MAX_FRAME_CELLS.
MAX_VIDEO_DIMENSION = 16384
MAX_FRAME_CELLS = 2**28
# Console text is always DejaVu Sans Mono at this size (R-REC-23). Libass draws
# its ASCII lines CONSOLE_FONT_SIZE pixels apart and its cells 9.30 pixels wide.
CONSOLE_FONT_SIZE = 18
# Hundredths of a pixel, rounded up so a long line can't outgrow its columns.
CELL_WIDTH_HUNDREDTHS = 931
MIN_TEXT_COLUMNS = 20
MIN_TEXT_ROWS = 3
# Side padding, and the title band plus bottom padding, around the text.
TEXT_SIDES = 32
TEXT_ENDS = 64
# Cells for the `seconds | ` line prefix.
PREFIX_CELLS = 10


def _exact(cell_scale: float) -> Fraction:
    """The decimal the caller wrote, so sizes don't depend on float rounding."""
    return Fraction(str(cell_scale))


def text_columns(panel_width: int, cell_scale: float) -> int:
    """Columns after the side padding and the `seconds | ` line prefix."""
    cell = CELL_WIDTH_HUNDREDTHS * _exact(cell_scale)
    return math.floor((panel_width - TEXT_SIDES) * 100 / cell) - PREFIX_CELLS


def text_rows(panel_height: int, cell_scale: float) -> int:
    """Rows below the panel title."""
    row = CONSOLE_FONT_SIZE * _exact(cell_scale)
    return math.floor((panel_height - TEXT_ENDS) / row)


def min_panel_width(cell_scale: float) -> int:
    """The narrowest panel with MIN_TEXT_COLUMNS columns."""
    cells = (MIN_TEXT_COLUMNS + PREFIX_CELLS) * CELL_WIDTH_HUNDREDTHS
    return TEXT_SIDES + math.ceil(cells * _exact(cell_scale) / 100)


def min_panel_height(cell_scale: float) -> int:
    """The shortest panel with MIN_TEXT_ROWS rows."""
    rows = MIN_TEXT_ROWS * CONSOLE_FONT_SIZE
    return TEXT_ENDS + math.ceil(rows * _exact(cell_scale))


def scale_text(cell_scale: float) -> str:
    return f"{cell_scale:.15g}"


@dataclass(frozen=True)
class ConsolePanel:
    """A requested panel. Its size is checked here; the display's in `video_layout`."""

    position: ConsolePosition = DEFAULT_CONSOLE_POSITION
    size: int = DEFAULT_CONSOLE_SIZE
    cell_scale: float = DEFAULT_CELL_SCALE

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
        if (
            type(self.cell_scale) not in (int, float)
            or not math.isfinite(self.cell_scale)
            or self.cell_scale < 1
        ):
            raise ValueError(
                "--console-cell-scale must be a finite number of at least 1."
            )
        scale = f"at --console-cell-scale {scale_text(self.cell_scale)}"
        if self.position in ("left", "right"):
            needed = min_panel_width(self.cell_scale)
            if self.size < needed:
                raise ValueError(
                    f"--console-size {self.size} is too narrow for {MIN_TEXT_COLUMNS} text "
                    f"columns {scale}; a {self.position} panel needs at least "
                    f"{needed + needed % 2}."
                )
        else:
            needed = min_panel_height(self.cell_scale)
            if self.size < needed:
                raise ValueError(
                    f"--console-size {self.size} is too short for {MIN_TEXT_ROWS} text "
                    f"rows {scale}; a {self.position} panel needs at least "
                    f"{needed + needed % 2}."
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
        if (
            not {"position", "size"}
            <= settings.keys()
            <= {
                "position",
                "size",
                "cell_scale",
            }
        ):
            raise ValueError(
                "console requires position and size, and allows only cell_scale."
            )
        return ConsolePanel(
            position=cast(ConsolePosition, settings["position"]),
            size=cast(int, settings["size"]),
            cell_scale=cast(float, settings.get("cell_scale", DEFAULT_CELL_SCALE)),
        )

    def parameters(self) -> dict[str, object]:
        """Omit the default scale, which an older runner would reject."""
        parameters: dict[str, object] = {"position": self.position, "size": self.size}
        if self.cell_scale != DEFAULT_CELL_SCALE:
            parameters["cell_scale"] = self.cell_scale
        return parameters


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
    cell_scale: float = DEFAULT_CELL_SCALE


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
    scale = f"at --console-cell-scale {scale_text(console.cell_scale)}"
    needed = min_panel_height(console.cell_scale)
    if panel_height < needed:
        return (
            f"A console panel needs a display at least {needed} pixels tall "
            f"for {MIN_TEXT_ROWS} text rows {scale}, but this display is "
            f"{width}x{height}. Use a taller display, a smaller --console-cell-scale, "
            "or --console-position top or bottom."
        )
    needed = min_panel_width(console.cell_scale)
    if panel_width < needed:
        return (
            f"A console panel needs a display at least {needed} pixels wide "
            f"for {MIN_TEXT_COLUMNS} text columns {scale}, but this display is "
            f"{width}x{height}. Use a wider display, a smaller --console-cell-scale, "
            "or --console-position left or right."
        )
    video_width = width + panel_width if beside else width
    video_height = height if beside else height + panel_height
    if max(video_width, video_height) > MAX_VIDEO_DIMENSION:
        return (
            f"A {console.size}-pixel {console.position} console panel makes the video "
            f"{video_width}x{video_height}, over the {MAX_VIDEO_DIMENSION}-pixel limit "
            f"of the encoder. Use a smaller --console-size."
        )
    if (video_width + 128) * (video_height + 128) >= MAX_FRAME_CELLS:
        return (
            f"A {console.size}-pixel {console.position} console panel makes the video "
            f"{video_width}x{video_height}, over FFmpeg's frame size limit. "
            "Use a smaller --console-size."
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
        console.cell_scale,
    )

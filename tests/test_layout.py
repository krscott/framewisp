import json
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from framewisp.captions import CAPTION_MODES, CaptionMode, render_recording
from framewisp.console import ConsoleCapture, console_frames
from framewisp.layout import (
    CONSOLE_POSITIONS,
    MAX_FRAME_CELLS,
    MAX_VIDEO_DIMENSION,
    ConsolePanel,
    ConsolePosition,
    Rect,
    VideoLayout,
    plain_layout,
    text_columns,
    text_rows,
    video_layout,
)
from framewisp.lib import recording_summary

GUI_SIZE = (640, 480)
GUI_COLOUR = 0x2060A0
PANEL_COLOUR = (0x16, 0x1B, 0x22)
# H.264 moves flat colours and sharp edges by less than this after a second encode.
TOLERANCE = 28


def panel_layout(
    display: tuple[int, int], position: ConsolePosition, pixels: int
) -> VideoLayout:
    layout = video_layout(display, ConsolePanel(position, pixels))
    assert isinstance(layout, VideoLayout)
    return layout


def source_video(path: Path, size: tuple[int, int]) -> None:
    """Write a still GUI with a distinct marker in each corner."""
    width, height = size
    boxes = [
        (0, 0, "red"),
        (width - 40, 0, "lime"),
        (0, height - 40, "yellow"),
        (width - 40, height - 40, "magenta"),
    ]
    markers = ",".join(
        f"drawbox=x={x}:y={y}:w=40:h=40:color={colour}:t=fill" for x, y, colour in boxes
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x{GUI_COLOUR:06x}:s={width}x{height}:r=30:d=2,{markers}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "1",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


def frame(video: Path, size: tuple[int, int], second: float = 0.5) -> Image.Image:
    data = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(second),
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    return Image.frombytes("RGB", size, data)


def crop(image: Image.Image, area: Rect) -> Image.Image:
    return image.crop((area.x, area.y, area.right, area.bottom))


def difference(first: Image.Image, second: Image.Image) -> int:
    assert first.size == second.size
    return max(ImageChops.difference(first, second).tobytes())


def ink_box(
    image: Image.Image, background: Image.Image
) -> tuple[int, int, int, int] | None:
    """Bound the pixels that clearly differ from a same-sized background."""
    mask = ImageChops.difference(image, background).convert("L")
    return ImageChops.subtract(mask, Image.new("L", mask.size, TOLERANCE)).getbbox()


def caption_events(text: str) -> str:
    return "".join(
        json.dumps(
            {
                "id": "typed",
                "event": event,
                "time": 10 + stamp,
                "action": "type",
                "parameters": {"text": text},
                "returncode": 0,
                "error": None,
            }
        )
        + "\n"
        for event, stamp in (("start", 0.2), ("end", 1.5))
    )


def render(
    tmp_path: Path,
    name: str,
    layout: VideoLayout,
    *,
    display: tuple[int, int] = GUI_SIZE,
    captions: CaptionMode = "none",
    caption: str = "",
) -> Image.Image:
    """Render a two-second still clip and return a frame from while a caption shows."""
    video = tmp_path / f"{name}.mp4"
    source_video(video, display)
    inputs = tmp_path / f"{name}.jsonl"
    inputs.write_text(caption_events(caption) if caption else "")
    console = tmp_path / "console.jsonl"
    if not console.exists():
        console.write_text("")
    render_recording(
        video,
        input_log=inputs,
        origin=10,
        stopped=12,
        log=tmp_path / "captions.log",
        captions=captions,
        click_pings=False,
        console=ConsoleCapture(tmp_path),
        layout=layout,
        env=dict(os.environ),
    )
    summary = recording_summary(video, env=dict(os.environ))
    assert (summary["width"], summary["height"]) == (layout.width, layout.height)
    return frame(video, (layout.width, layout.height))


# R-REC-12, R-REC-13
@pytest.mark.parametrize(
    ("position", "pixels", "video", "gui", "panel"),
    [
        ("right", 640, (1280, 480), Rect(0, 0, 640, 480), Rect(640, 0, 640, 480)),
        ("right", 400, (1040, 480), Rect(0, 0, 640, 480), Rect(640, 0, 400, 480)),
        ("left", 640, (1280, 480), Rect(640, 0, 640, 480), Rect(0, 0, 640, 480)),
        ("left", 400, (1040, 480), Rect(400, 0, 640, 480), Rect(0, 0, 400, 480)),
        ("top", 640, (640, 1120), Rect(0, 640, 640, 480), Rect(0, 0, 640, 640)),
        ("top", 200, (640, 680), Rect(0, 200, 640, 480), Rect(0, 0, 640, 200)),
        ("bottom", 640, (640, 1120), Rect(0, 0, 640, 480), Rect(0, 480, 640, 640)),
        ("bottom", 200, (640, 680), Rect(0, 0, 640, 480), Rect(0, 480, 640, 200)),
    ],
)
def test_layout_places_panel_and_gui(
    position: ConsolePosition,
    pixels: int,
    video: tuple[int, int],
    gui: Rect,
    panel: Rect,
) -> None:
    layout = panel_layout(GUI_SIZE, position, pixels)
    assert (layout.width, layout.height) == video
    assert layout.gui == gui
    assert layout.panel == panel


# R-REC-12, R-REC-13
def test_console_panel_defaults_to_a_wide_right_panel() -> None:
    assert ConsolePanel() == ConsolePanel("right", 640)


def test_layout_without_a_panel_fills_the_video_with_the_gui() -> None:
    assert video_layout(GUI_SIZE, None) == plain_layout(GUI_SIZE)
    assert plain_layout(GUI_SIZE) == VideoLayout(640, 480, Rect(0, 0, 640, 480))


def test_default_panel_keeps_its_text_capacity() -> None:
    assert (text_columns(640), text_rows(720)) == (41, 27)


# R-REC-16
@pytest.mark.parametrize(
    ("position", "size", "message"),
    [
        ("left", 641, "must be even"),
        ("top", 201, "must be even"),
        ("right", 0, "positive even"),
        ("right", -2, "positive even"),
        ("right", True, "positive even"),
        ("right", "640", "positive even"),
        ("right", 16386, "at most 16384"),
        pytest.param("top", 10**4400, "at most 16384", id="unformattable"),
        ("left", 388, "at least 390"),
        ("right", 2, "at least 390"),
        ("top", 134, "at least 136"),
        ("bottom", 2, "at least 136"),
        ("middle", 640, "must be one of"),
    ],
)
def test_panel_rejects_invalid_values(
    position: ConsolePosition, size: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ConsolePanel(position, size)


# R-REC-16
@pytest.mark.parametrize(
    ("position", "size"), [("left", 390), ("right", 390), ("top", 136), ("bottom", 136)]
)
def test_smallest_panel_has_the_minimum_text_capacity(
    position: ConsolePosition, size: int
) -> None:
    panel = panel_layout((390, 136), position, size).panel
    assert panel is not None
    assert text_columns(panel.width) == 20
    assert text_rows(panel.height) == 3


# R-REC-16
@pytest.mark.parametrize(
    ("display", "position", "size", "named"),
    [
        ((1280, 134), "right", 640, ["1280x134", "136", "taller", "top or bottom"]),
        ((1280, 100), "left", 640, ["1280x100", "136", "taller", "top or bottom"]),
        ((388, 480), "top", 200, ["388x480", "390", "wider", "left or right"]),
        ((320, 240), "bottom", 200, ["320x240", "390", "wider", "left or right"]),
    ],
)
def test_layout_names_the_display_when_the_following_dimension_is_too_small(
    display: tuple[int, int],
    position: ConsolePosition,
    size: int,
    named: list[str],
) -> None:
    error = video_layout(display, ConsolePanel(position, size))
    assert isinstance(error, str)
    assert all(text in error for text in named), error


# R-REC-16
@pytest.mark.parametrize("position", CONSOLE_POSITIONS)
def test_layout_keeps_the_video_within_the_encoder_limit(
    position: ConsolePosition,
) -> None:
    fitting = MAX_VIDEO_DIMENSION - 640
    assert isinstance(
        video_layout((640, 640), ConsolePanel(position, fitting)), VideoLayout
    )
    error = video_layout((640, 640), ConsolePanel(position, fitting + 2))
    assert isinstance(error, str)
    assert str(MAX_VIDEO_DIMENSION) in error
    assert "--console-size" in error


# R-REC-16
@pytest.mark.parametrize(
    ("display", "position", "fitting"),
    [
        ((16384, 8192), "top", 7936),
        ((16384, 8192), "bottom", 7936),
        ((8192, 16384), "left", 7936),
        ((8192, 16384), "right", 7936),
    ],
)
def test_layout_keeps_the_video_within_the_frame_size_limit(
    display: tuple[int, int], position: ConsolePosition, fitting: int
) -> None:
    assert isinstance(
        video_layout(display, ConsolePanel(position, fitting)), VideoLayout
    )
    error = video_layout(display, ConsolePanel(position, fitting + 2))
    assert isinstance(error, str)
    assert "frame size limit" in error
    assert "--console-size" in error


# R-REC-16
@pytest.mark.parametrize(
    ("width", "height", "encodes"),
    [
        (16384, 16128, True),
        (16384, 16130, False),
        (16258, 16254, True),
        (16256, 16256, False),
    ],
)
def test_frame_size_limit_matches_ffmpeg(
    tmp_path: Path, width: int, height: int, encodes: bool
) -> None:
    assert ((width + 128) * (height + 128) < MAX_FRAME_CELLS) is encodes
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=30",
            "-vf",
            f"pad={width}:{height}:0:0",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(tmp_path / "frame.mp4"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert (result.returncode == 0) is encodes, result.stderr


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ({"position": "top", "size": 200}, ConsolePanel("top", 200)),
    ],
)
def test_request_value_round_trips(raw: object, expected: ConsolePanel | None) -> None:
    assert ConsolePanel.parse(raw) == expected
    if expected is not None:
        assert expected.parameters() == raw


@pytest.mark.parametrize(
    "raw",
    [
        True,
        "right",
        {"position": "left"},
        {"position": "left", "size": 640, "extra": 1},
        {"position": "left", "size": 16386},
        pytest.param({"position": "left", "size": 10**4400}, id="unformattable"),
    ],
)
def test_request_value_rejects_other_shapes(raw: object) -> None:
    with pytest.raises(ValueError, match="console"):
        ConsolePanel.parse(raw)


PLACEMENTS: list[tuple[ConsolePosition, int]] = [
    ("right", 640),
    ("right", 392),
    ("left", 640),
    ("left", 392),
    ("top", 640),
    ("top", 200),
    ("bottom", 640),
    ("bottom", 200),
]


# R-REC-12, R-REC-13, R-REC-14
@pytest.mark.parametrize(("position", "pixels"), PLACEMENTS)
def test_panel_extends_the_video_around_an_unchanged_gui(
    tmp_path: Path, position: ConsolePosition, pixels: int
) -> None:
    plain = tmp_path / "plain.mp4"
    source_video(plain, GUI_SIZE)
    original = frame(plain, GUI_SIZE)
    layout = panel_layout(GUI_SIZE, position, pixels)
    assert layout.panel is not None
    video = render(tmp_path, "panel", layout)
    assert difference(crop(video, layout.gui), original) <= TOLERANCE
    panel = crop(video, layout.panel)
    background = Image.new("RGB", panel.size, PANEL_COLOUR)
    title = ink_box(panel, background)
    assert title is not None
    # Only the title is drawn, at the panel's own top left.
    assert 14 <= title[0] <= 20 and 14 <= title[1] <= 24
    assert title[3] <= 44


# R-REC-15
@pytest.mark.parametrize(("position", "pixels"), PLACEMENTS)
def test_caption_stays_centred_on_the_gui(
    tmp_path: Path, position: ConsolePosition, pixels: int
) -> None:
    layout = panel_layout(GUI_SIZE, position, pixels)
    assert layout.panel is not None
    video = render(tmp_path, "captioned", layout, captions="keys", caption="centred")
    bare = render(tmp_path, "bare", layout)
    box = ink_box(crop(video, layout.gui), crop(bare, layout.gui))
    assert box is not None
    assert (box[0] + box[2]) / 2 == pytest.approx(GUI_SIZE[0] / 2, abs=3)
    # The caption sits at the bottom of the GUI, not of the extended video.
    assert GUI_SIZE[1] - 60 <= box[3] <= GUI_SIZE[1]
    assert difference(crop(video, layout.panel), crop(bare, layout.panel)) <= TOLERANCE


# R-REC-15
@pytest.mark.parametrize("position", ["top", "bottom"])
def test_panel_height_does_not_resize_a_caption(
    tmp_path: Path, position: ConsolePosition
) -> None:
    layout = panel_layout(GUI_SIZE, position, 640)
    plain = plain_layout(GUI_SIZE)
    video = render(tmp_path, "panel", layout, captions="keys", caption="same size")
    bare = render(tmp_path, "bare", layout)
    alone = render(tmp_path, "alone", plain, captions="keys", caption="same size")
    reference = render(tmp_path, "reference", plain)
    box = ink_box(crop(video, layout.gui), crop(bare, layout.gui))
    expected = ink_box(alone, reference)
    assert box is not None and expected is not None
    assert box[2] - box[0] == pytest.approx(expected[2] - expected[0], abs=2)
    assert box[3] - box[1] == pytest.approx(expected[3] - expected[1], abs=2)


# R-REC-15
@pytest.mark.parametrize(
    ("position", "pixels"),
    [("left", 640), ("right", 640), ("top", 200), ("bottom", 200)],
)
def test_wide_caption_never_reaches_the_panel(
    tmp_path: Path, position: ConsolePosition, pixels: int
) -> None:
    display = (400, 1000)
    layout = panel_layout(display, position, pixels)
    assert layout.panel is not None
    video = render(
        tmp_path,
        "captioned",
        layout,
        display=display,
        captions="keys",
        caption="W" * 70,
    )
    bare = render(tmp_path, "bare", layout, display=display)
    box = ink_box(crop(video, layout.gui), crop(bare, layout.gui))
    assert box is not None
    # The caption is wider than the GUI, so the clip decides where it stops.
    assert box[0] == 0 and box[2] == display[0]
    assert difference(crop(video, layout.panel), crop(bare, layout.panel)) <= TOLERANCE


# R-REC-14, R-REC-15
@pytest.mark.parametrize("captions", CAPTION_MODES)
@pytest.mark.parametrize("position", CONSOLE_POSITIONS)
def test_every_caption_mode_keeps_the_panel_and_gui(
    tmp_path: Path, position: ConsolePosition, captions: CaptionMode
) -> None:
    (tmp_path / "console.jsonl").write_text(
        json.dumps({"time": 10.1, "text": "panel output\n"}) + "\n"
    )
    layout = panel_layout(GUI_SIZE, position, 400)
    assert layout.panel is not None
    video = render(tmp_path, "mode", layout, captions=captions, caption="typed")
    bare = render(tmp_path, "bare", layout)
    shown = ink_box(crop(video, layout.gui), crop(bare, layout.gui)) is not None
    assert shown is (captions in {"keys", "all"})
    assert difference(crop(video, layout.panel), crop(bare, layout.panel)) <= TOLERANCE
    panel = crop(video, layout.panel)
    output = ink_box(panel, Image.new("RGB", panel.size, PANEL_COLOUR))
    assert output is not None and output[3] > 48


def test_text_wraps_to_the_panel_and_keeps_the_newest_rows(tmp_path: Path) -> None:
    log = tmp_path / "console.jsonl"
    log.write_text(json.dumps({"time": 10.1, "text": "x" * 100 + "\nlast\n"}) + "\n")
    panel = Rect(0, 0, 390, 136)
    frames = list(console_frames(log, origin=10, stopped=12, panel=panel))
    lines = frames[-1].text.split("\n")
    assert len(lines) == 3
    assert [line.split(" | ")[1] for line in lines] == ["x" * 20, "x" * 20, "last"]


@pytest.mark.parametrize(("height", "rows"), [(136, 3), (200, 5)])
@pytest.mark.parametrize("position", ["top", "bottom"])
def test_short_panel_draws_only_the_rows_that_fit(
    tmp_path: Path, position: ConsolePosition, height: int, rows: int
) -> None:
    (tmp_path / "console.jsonl").write_text(
        "".join(
            json.dumps({"time": 10.1, "text": f"row {index}\n"}) + "\n"
            for index in range(10)
        )
    )
    layout = panel_layout(GUI_SIZE, position, height)
    assert layout.panel is not None
    video = render(tmp_path, "rows", layout)
    panel = crop(video, layout.panel)
    background = Image.new("RGB", panel.size, PANEL_COLOUR)
    mask = ImageChops.difference(panel, background).convert("L")
    inked = [
        max(mask.crop((0, y, mask.width, y + 1)).tobytes()) > TOLERANCE
        for y in range(44, mask.height)
    ]
    bands = sum(1 for above, row in zip([False, *inked], inked) if row and not above)
    assert bands == rows


@pytest.mark.parametrize(
    ("first", "second", "pixels"), [("top", "bottom", 200), ("left", "right", 400)]
)
def test_panel_text_does_not_depend_on_the_edge(
    tmp_path: Path, first: ConsolePosition, second: ConsolePosition, pixels: int
) -> None:
    (tmp_path / "console.jsonl").write_text(
        json.dumps({"time": 10.1, "text": "M" * 100 + "\n"}) + "\n"
    )
    panels: list[Image.Image] = []
    for position in (first, second):
        layout = panel_layout(GUI_SIZE, position, pixels)
        assert layout.panel is not None
        panels.append(crop(render(tmp_path, position, layout), layout.panel))
    assert difference(*panels) <= TOLERANCE
    background = Image.new("RGB", panels[0].size, PANEL_COLOUR)
    box = ink_box(panels[0], background)
    assert box is not None
    # Wrapping keeps text inside the panel instead of running into its clip.
    assert box[2] < panels[0].width - 16

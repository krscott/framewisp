import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from framewisp.captions import (
    CAPTION_MODES,
    DEFAULT_CAPTIONS,
    CaptionMode,
    log_input,
    render_recording,
    subtitle_script,
)
from framewisp.compact import recording_duration, retained_frames
from framewisp.console import ConsoleCapture
from framewisp.layout import (
    ConsolePanel,
    ConsolePosition,
    VideoLayout,
    plain_layout,
    video_layout,
)
from framewisp.pings import HOLD_RADIUS, PING_SECONDS, Hold, Ping, pings_for_clip

SIZE = (1280, 720)
ORIGIN = 100.0
STOPPED = 106.0


def panel_layout(position: ConsolePosition, pixels: int = 640) -> VideoLayout:
    layout = video_layout(SIZE, ConsolePanel(position, pixels))
    assert isinstance(layout, VideoLayout)
    return layout


def ping_line(
    time: float, x: int, y: int, *, action: str = "click", identifier: str = "command"
) -> str:
    return (
        json.dumps(
            {
                "id": identifier,
                "event": "ping",
                "time": ORIGIN + time,
                "action": action,
                "parameters": {},
                "returncode": None,
                "error": None,
                "x": x,
                "y": y,
            }
        )
        + "\n"
    )


def drag_lines(
    press: float,
    start: tuple[int, int],
    release: float | None,
    end: tuple[int, int],
) -> str:
    """Log a drag's press and, unless it was interrupted, its release."""
    identifier = f"drag-{press}"
    lines = ping_line(press, *start, action="drag", identifier=identifier)
    if release is not None:
        lines += ping_line(release, *end, action="drag", identifier=identifier)
    return lines


def command_lines(time: float, end: float) -> str:
    return "".join(
        json.dumps(
            {
                "id": f"command-{time}",
                "event": event,
                "time": ORIGIN + stamp,
                "action": "click",
                "parameters": {"x": 1, "y": 1, "button": "left", "count": 1},
                "returncode": 0,
                "error": None,
            }
        )
        + "\n"
        for event, stamp in (("start", time), ("end", end))
    )


@pytest.fixture
def raw_video(tmp_path: Path) -> Path:
    video = tmp_path / "clip with spaces.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=white:s={SIZE[0]}x{SIZE[1]}:r=30:d=6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return video


def frames(
    video: Path, first: int, last: int, *, size: tuple[int, int] = SIZE
) -> list[Image.Image]:
    """Decode inclusive frame numbers from a constant 30 fps clip."""
    data = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            f"select='between(n,{first},{last})'",
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    stride = size[0] * size[1] * 3
    return [
        Image.frombytes("RGB", size, data[offset : offset + stride])
        for offset in range(0, len(data), stride)
    ]


def red_box(frame: Image.Image) -> tuple[int, int, int, int] | None:
    """Bound the pixels a fading red ring tints, on a white or dark background."""
    red, green, blue = (band.tobytes() for band in frame.split())
    points = [
        index
        for index, (r, g, b) in enumerate(zip(red, green, blue))
        if r - g > 40 and r - b > 40
    ]
    if not points:
        return None
    xs = [index % frame.width for index in points]
    ys = [index // frame.width for index in points]
    return min(xs), min(ys), max(xs), max(ys)


def render(video: Path, log: Path, **options: object) -> None:
    render_recording(
        video,
        input_log=log,
        origin=ORIGIN,
        stopped=STOPPED,
        log=log.parent / "captions.log",
        env=dict(os.environ),
        **({"layout": plain_layout(SIZE), "console": ConsoleCapture(log.parent)} | options),  # type: ignore[arg-type]
    )


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "inputs.jsonl"


def test_clip_keeps_pings_inside_its_bounds_and_skips_partial_lines(log: Path) -> None:
    log.write_text(
        ping_line(-0.01, 1, 1)
        + command_lines(0.5, 0.6)
        + ping_line(0, 2, 2)
        + ping_line(5.99, 3, 3)
        + ping_line(6, 4, 4)
        + ping_line(1, 5, 5)[:20]
    )
    assert pings_for_clip(log, origin=ORIGIN, stopped=STOPPED) == [
        Ping(0, 2, 2),
        Ping(pytest.approx(5.99), 3, 3),  # type: ignore[arg-type]
    ]


# R-REC-6
@pytest.mark.parametrize("second", [1.01, 1.03])
def test_ring_is_centred_and_starts_within_the_tolerance(
    raw_video: Path, log: Path, second: float
) -> None:
    log.write_text(ping_line(second, 300, 200))
    render(raw_video, log, captions="none")
    decoded = frames(raw_video, 28, 50)
    shown = [
        (28 + index, box)
        for index, frame in enumerate(decoded)
        if (box := red_box(frame)) is not None
    ]
    first, _ = shown[0]
    assert 0 <= first / 30 - second <= 0.05
    for _, (left, top, right, bottom) in shown:
        assert (left + right) / 2 == pytest.approx(300, abs=1)
        assert (top + bottom) / 2 == pytest.approx(200, abs=1)
    pixel = decoded[first + 3 - 28].getpixel((300, 200))
    assert isinstance(pixel, tuple) and min(pixel) > 240
    widths = [box[2] - box[0] for _, box in shown]
    assert widths == sorted(widths) and widths[-1] > 2 * widths[0]
    assert shown[-1][0] / 30 < second + PING_SECONDS


# R-REC-5
def test_double_click_draws_two_concurrent_rings(raw_video: Path, log: Path) -> None:
    log.write_text(ping_line(1, 300, 200) + ping_line(1.2, 300, 200))
    render(raw_video, log, captions="none")
    frame = frames(raw_video, 37, 37)[0]
    row = [frame.getpixel((x, 200)) for x in range(240, 301)]
    edges = [
        x
        for x, (left, right) in enumerate(zip(row, row[1:]))
        if isinstance(left, tuple)
        and isinstance(right, tuple)
        and (left[0] - left[1] > 40) != (right[0] - right[1] > 40)
    ]
    assert len(edges) == 4


# R-REC-5
def test_drags_pair_their_press_and_release(log: Path) -> None:
    log.write_text(
        drag_lines(1, (10, 10), 2, (50, 10))
        + ping_line(1.5, 7, 7)
        + drag_lines(3, (20, 20), None, (0, 0))
        + drag_lines(-3, (30, 30), -2, (60, 30))
        + drag_lines(-1, (100, 40), 1, (300, 40))
        + drag_lines(6, (40, 40), 7, (80, 40))
    )
    assert pings_for_clip(log, origin=ORIGIN, stopped=STOPPED) == [
        Hold(1, 10, 10, 2, 50, 10, grows=True),
        Hold(0, 200, 40, 1, 300, 40, grows=False),
        Ping(1.5, 7, 7),
        Ping(1, 10, 10),
        Ping(3, 20, 20),
    ]


def is_tinted(frame: Image.Image, x: int, y: int) -> bool:
    pixel = frame.getpixel((x, y))
    assert isinstance(pixel, tuple)
    return pixel[0] - pixel[1] > 40 and pixel[0] - pixel[2] > 40


def green(frame: Image.Image, x: int, y: int) -> int:
    pixel = frame.getpixel((x, y))
    assert isinstance(pixel, tuple)
    return int(pixel[1])


def assert_disc(
    frame: Image.Image, x: float, y: float, *, offset: tuple[int, int] = (0, 0)
) -> None:
    """Check for only a filled disc, no wider ring, centred on (x, y) in the GUI."""
    box = red_box(frame)
    assert box is not None
    left, top, right, bottom = box
    assert (left + right) / 2 == pytest.approx(offset[0] + x, abs=2)
    assert (top + bottom) / 2 == pytest.approx(offset[1] + y, abs=2)
    assert right - left <= 2 * HOLD_RADIUS + 2
    assert is_tinted(frame, offset[0] + round(x), offset[1] + round(y))


# R-REC-5, R-REC-6
def test_hold_disc_follows_the_pointer_and_fades_after_the_release(
    raw_video: Path, log: Path
) -> None:
    log.write_text(drag_lines(1, (300, 200), 2, (700, 200)))
    render(raw_video, log, captions="none")
    decoded = dict(zip(range(29, 77), frames(raw_video, 29, 76)))
    assert red_box(decoded[29]) is None
    # A filled disc, unlike the press ring, tints the pointer position at once.
    assert is_tinted(decoded[31], 313, 200)
    for index in range(46, 60):  # After the ring, until the release.
        assert_disc(decoded[index], 300 + 400 * (index / 30 - 1), 200)
    for index in range(60, 64):  # The disc stays at the release.
        assert_disc(decoded[index], 700, 200)
    for index in range(64, 77):  # No ring grows out of the release.
        box = red_box(decoded[index])
        assert box is None or box[2] - box[0] <= 2 * HOLD_RADIUS + 2
    # Fading starts within 0.05 seconds of the release.
    assert green(decoded[62], 700, 200) > green(decoded[59], 690, 200) + 5
    assert red_box(decoded[76]) is None


# R-REC-5
def test_hold_from_before_the_clip_shows_from_its_first_frame(
    raw_video: Path, log: Path
) -> None:
    log.write_text(drag_lines(-1, (100, 200), 1, (500, 200)))
    render(raw_video, log, captions="none")
    decoded = frames(raw_video, 0, 15)
    assert_disc(decoded[0], 300, 200)
    assert_disc(decoded[15], 400, 200)


# R-REC-5
def test_instant_drag_fades_at_its_release_position(raw_video: Path, log: Path) -> None:
    log.write_text(drag_lines(1, (300, 200), 1, (500, 200)))
    render(raw_video, log, captions="none")
    # The press ring stays at x=300; crop it away to measure only the disc while
    # it is still clearly tinted.
    for frame in frames(raw_video, 33, 36):
        box = red_box(frame.crop((420, 100, 580, 300)))
        assert box is not None
        assert (box[0] + box[2]) / 2 == pytest.approx(80, abs=2)


def compacted_ring_frames(video: Path, *, size: tuple[int, int]) -> dict[int, int]:
    """Map frames showing a ring in the GUI area to that ring's width."""
    shown: dict[int, tuple[int, int, int, int]] = {}
    for index, frame in enumerate(frames(video, 0, 200, size=size)):
        box = red_box(frame.crop((0, 0, SIZE[0], SIZE[1])))
        if box is not None:
            shown[index] = box
    return {index: box[2] - box[0] for index, box in shown.items()}


def ring_centre(
    video: Path, index: int, *, size: tuple[int, int]
) -> tuple[float, float]:
    box = red_box(frames(video, index, index, size=size)[0])
    assert box is not None
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


# R-REC-8, R-REC-9
@pytest.mark.parametrize("position", [None, "right", "left", "bottom"])
def test_compaction_maps_pings_to_retained_frames(
    raw_video: Path, log: Path, tmp_path: Path, position: ConsolePosition | None
) -> None:
    (tmp_path / "console.jsonl").write_text("")
    (tmp_path / "activity.jsonl").write_text("")
    log.write_text(command_lines(4, 4.3) + ping_line(4.25, 600, 300))
    layout = plain_layout(SIZE) if position is None else panel_layout(position)
    ranges = retained_frames(
        (log, tmp_path / "activity.jsonl"),
        origin=ORIGIN,
        stopped=STOPPED,
        duration=recording_duration(raw_video, env=dict(os.environ)),
    )
    source = [frame for first, last in ranges for frame in range(first, last)]
    render(
        raw_video,
        log,
        captions="none",
        compact=True,
        layout=layout,
    )
    size = (layout.width, layout.height)
    shown = compacted_ring_frames(raw_video, size=size)
    first = source.index(math.ceil(4.25 * 30))
    # The render keeps the whole animation, not only its opening frames.
    assert source[first + 14] == source[first] + 14
    assert round(recording_duration(raw_video, env=dict(os.environ)) * 30) == len(
        source
    )
    assert first <= min(shown) <= first + 1
    assert ring_centre(raw_video, min(shown) + 2, size=size) == pytest.approx(
        (layout.gui.x + 600, layout.gui.y + 300), abs=1
    )
    assert source[max(shown)] / 30 > 4.25 + 0.3


# R-REC-8
def test_padding_retains_every_ping_frame(log: Path) -> None:
    (log.parent / "activity.jsonl").write_text("")
    log.write_text(command_lines(10, 10.01))
    ranges = retained_frames(
        (log, log.parent / "activity.jsonl"),
        origin=ORIGIN,
        stopped=ORIGIN + 30,
        duration=30,
    )
    kept = {frame for first, last in ranges for frame in range(first, last)}
    assert set(range(300, math.ceil((10.005 + PING_SECONDS) * 30))) <= kept


# R-REC-8
def test_clip_end_cuts_a_late_ping_short(raw_video: Path, log: Path) -> None:
    (log.parent / "activity.jsonl").write_text("")
    log.write_text(command_lines(5.7, 5.71) + ping_line(5.8, 300, 200))
    render(raw_video, log, captions="none", compact=True)
    shown = compacted_ring_frames(raw_video, size=SIZE)
    assert 4 <= len(shown) < 15
    assert (
        max(shown)
        == round(recording_duration(raw_video, env=dict(os.environ)) * 30) - 1
    )


# R-REC-8
def test_compaction_keeps_each_ping_whole(raw_video: Path, log: Path) -> None:
    (log.parent / "activity.jsonl").write_text("")
    log.write_text(command_lines(3, 3.01) + ping_line(3.005, 300, 200))
    render(
        raw_video,
        log,
        captions="none",
        compact=True,
    )
    shown = compacted_ring_frames(raw_video, size=SIZE)
    # The fading ring outgrows the 28-pixel opening ring only late in its animation.
    assert max(shown.values()) > 60
    assert max(shown) - min(shown) >= 10


# R-REC-9
@pytest.mark.parametrize(
    ("position", "x", "y"),
    [
        ("left", 5, 400),
        ("right", SIZE[0] - 5, 400),
        ("top", 600, 5),
        ("bottom", 600, SIZE[1] - 5),
    ],
)
@pytest.mark.parametrize("pixels", [640, 400])
def test_ring_clips_at_the_console_panel(
    raw_video: Path,
    log: Path,
    tmp_path: Path,
    position: ConsolePosition,
    pixels: int,
    x: int,
    y: int,
) -> None:
    (tmp_path / "console.jsonl").write_text("")
    log.write_text(ping_line(1, x, y) + ping_line(3, 600, 300))
    layout = panel_layout(position, pixels)
    assert layout.panel is not None
    render(raw_video, log, captions="none", layout=layout)
    size = (layout.width, layout.height)
    edge = frames(raw_video, 36, 36, size=size)[0]
    inside = frames(raw_video, 96, 96, size=size)[0]
    gui, panel = layout.gui, layout.panel
    assert red_box(edge.crop((gui.x, gui.y, gui.right, gui.bottom))) is not None
    assert red_box(edge.crop((panel.x, panel.y, panel.right, panel.bottom))) is None
    # The ring reaches the shared edge, so the clip sits on it rather than inside it.
    box = red_box(edge)
    assert box is not None
    if position == "left":
        assert box[0] == gui.x
    elif position == "right":
        assert box[2] == gui.right - 1
    elif position == "top":
        assert box[1] == gui.y
    else:
        assert box[3] == gui.bottom - 1
    box = red_box(inside)
    assert box is not None
    assert (box[0] + box[2]) / 2 == pytest.approx(gui.x + 600, abs=1)
    assert (box[1] + box[3]) / 2 == pytest.approx(gui.y + 300, abs=1)


# R-REC-8
def test_compaction_keeps_the_hold_path_and_fade(
    raw_video: Path, log: Path, tmp_path: Path
) -> None:
    (tmp_path / "activity.jsonl").write_text("")
    log.write_text(command_lines(3, 4) + drag_lines(3, (300, 200), 4, (700, 200)))
    ranges = retained_frames(
        (log, tmp_path / "activity.jsonl"),
        origin=ORIGIN,
        stopped=STOPPED,
        duration=recording_duration(raw_video, env=dict(os.environ)),
    )
    source = [frame for first, last in ranges for frame in range(first, last)]
    assert source[0] == 0 and 90 not in source[:30]  # The clip is cut before 3 s.
    render(raw_video, log, captions="none", compact=True)
    decoded = frames(raw_video, 0, len(source) - 1)
    for frame in range(108, 120):  # After the ring, until the release.
        assert_disc(decoded[source.index(frame)], 300 + 400 * (frame / 30 - 3), 200)
    # The fade keeps its full duration: a faint tint remains 0.4 seconds in.
    assert green(decoded[source.index(132)], 700, 200) < 250


# R-REC-9
@pytest.mark.parametrize(
    ("position", "x", "y"),
    [
        ("left", 5, 400),
        ("right", SIZE[0] - 5, 400),
        ("top", 600, 5),
        ("bottom", 600, SIZE[1] - 5),
    ],
)
def test_hold_disc_clips_at_the_console_panel(
    raw_video: Path,
    log: Path,
    tmp_path: Path,
    position: ConsolePosition,
    x: int,
    y: int,
) -> None:
    (tmp_path / "console.jsonl").write_text("")
    log.write_text(drag_lines(1, (x, y), 2, (600, 300)))
    layout = panel_layout(position, 400)
    assert layout.panel is not None
    render(raw_video, log, captions="none", layout=layout)
    gui, panel = layout.gui, layout.panel
    decoded = frames(raw_video, 30, 75, size=(layout.width, layout.height))
    for frame in decoded:
        assert (
            red_box(frame.crop((panel.x, panel.y, panel.right, panel.bottom))) is None
        )
    # The disc starts against the shared edge, then leaves it.
    assert red_box(decoded[1].crop((gui.x, gui.y, gui.right, gui.bottom))) is not None
    fraction = 54 / 30 - 1
    assert_disc(
        decoded[24],
        x + (600 - x) * fraction,
        y + (300 - y) * fraction,
        offset=(gui.x, gui.y),
    )


# R-REC-11
@pytest.mark.parametrize("captions", CAPTION_MODES)
def test_pings_ignore_the_caption_mode(
    raw_video: Path, log: Path, captions: CaptionMode
) -> None:
    log.write_text(
        command_lines(1, 1.1)
        + ping_line(1.05, 300, 200)
        + drag_lines(2, (300, 200), 3, (700, 200))
    )
    render(raw_video, log, captions=captions)
    ring, disc = frames(raw_video, 36, 36)[0], frames(raw_video, 78, 78)[0]
    assert red_box(ring) is not None
    assert_disc(disc, 540, 200)


def test_default_mode_pings_a_clip_without_keyboard_input(
    raw_video: Path, log: Path
) -> None:
    log.write_text(command_lines(1, 1.1) + ping_line(1.05, 300, 200))
    render(raw_video, log)
    assert red_box(frames(raw_video, 36, 36)[0]) is not None


# R-REC-7
@pytest.mark.parametrize("captions", CAPTION_MODES)
def test_opt_out_applies_with_every_caption_mode(
    raw_video: Path, log: Path, captions: CaptionMode
) -> None:
    log.write_text(
        command_lines(1, 1.1)
        + ping_line(1.05, 300, 200)
        + drag_lines(2, (300, 200), 3, (700, 200))
    )
    render(raw_video, log, captions=captions, click_pings=False)
    assert red_box(frames(raw_video, 36, 36)[0]) is None
    assert red_box(frames(raw_video, 78, 78)[0]) is None


# R-REC-7
def test_disabled_pings_leave_the_raw_recording_untouched(
    raw_video: Path, log: Path
) -> None:
    before = raw_video.read_bytes()
    log.write_text(ping_line(1, 300, 200))
    render(raw_video, log, captions="none", click_pings=False)
    assert raw_video.read_bytes() == before


def test_clip_without_presses_is_not_encoded_again(raw_video: Path, log: Path) -> None:
    before = raw_video.read_bytes()
    log.write_text("")
    render(raw_video, log, captions="none")
    assert raw_video.read_bytes() == before


def test_script_has_one_dialogue_per_ping() -> None:
    script = subtitle_script(
        [], pings=[Ping(1, 2, 3), Ping(2, 4, 5)], layout=plain_layout(SIZE)
    )
    assert script.count("Dialogue: 1,") == 2
    assert script.count("\\pos(2,3)") == 1


def assert_raw_clip_survived(video: Path, before: bytes, error: str) -> None:
    assert video.read_bytes() == before
    assert str(video) in error
    assert "captions.log" in error
    assert "inputs.jsonl" in error
    assert "raw video remains" in error
    assert not list(video.parent.glob(".framewisp-captions-*"))
    assert len(frames(video, 0, 179)) == 180


# R-REC-10
@pytest.mark.parametrize("captions", ["none", DEFAULT_CAPTIONS])
def test_failed_encoder_leaves_pings_only_clip_playable(
    raw_video: Path,
    log: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captions: CaptionMode,
) -> None:
    before = raw_video.read_bytes()
    log.write_text(ping_line(1, 300, 200))
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "ffmpeg").write_text(f"#!{shutil.which('sh')}\nexit 3\n")
    (tools / "ffmpeg").chmod(0o755)
    path = str(tools) + ":" + os.environ["PATH"]
    with monkeypatch.context() as patch:
        patch.setenv("PATH", path)
        with pytest.raises(RuntimeError) as failure:
            render(raw_video, log, captions=captions)
    assert "status 3" in str(failure.value)
    assert_raw_clip_survived(raw_video, before, str(failure.value))


# R-REC-10
@pytest.mark.parametrize("captions", ["none", DEFAULT_CAPTIONS])
def test_malformed_log_leaves_pings_only_clip_playable(
    raw_video: Path, log: Path, captions: CaptionMode
) -> None:
    before = raw_video.read_bytes()
    log.write_text("not json\n")
    with pytest.raises(RuntimeError) as failure:
        render(raw_video, log, captions=captions)
    assert_raw_clip_survived(raw_video, before, str(failure.value))


COMPLETE_RECORDS = (command_lines(1, 1.1) + ping_line(1.05, 300, 200)).encode()
UNFINISHED = b'{"id": "unfini'

# Each entry makes exactly one reader consume the log, so each is held to the rule.
LOG_READERS = {
    "captions": {"captions": "all", "click_pings": False},
    "pings": {"captions": "none"},
    "compaction": {
        "captions": "none",
        "click_pings": False,
        "compact": True,
    },
}


# R-LOG-8, R-REC-10
@pytest.mark.parametrize("reader", LOG_READERS.values(), ids=LOG_READERS)
@pytest.mark.parametrize(
    "bad_log",
    [
        b"not json\n" + COMPLETE_RECORDS + UNFINISHED,
        b"not json\r" + COMPLETE_RECORDS + UNFINISHED,
        b"1\n" + COMPLETE_RECORDS + UNFINISHED,
        b"{}\n" + COMPLETE_RECORDS + UNFINISHED,
        COMPLETE_RECORDS + UNFINISHED + b"\n",
    ],
    ids=["text", "carriage-return", "not-an-object", "no-fields", "terminated-last"],
)
def test_malformed_complete_line_fails_every_reader_and_keeps_the_clip(
    raw_video: Path, log: Path, reader: dict[str, object], bad_log: bytes
) -> None:
    before = raw_video.read_bytes()
    (log.parent / "activity.jsonl").write_text("")
    log.write_bytes(bad_log)
    with pytest.raises(RuntimeError) as failure:
        render(raw_video, log, **reader)
    assert_raw_clip_survived(raw_video, before, str(failure.value))


# R-LOG-7
@pytest.mark.parametrize("reader", LOG_READERS.values(), ids=LOG_READERS)
def test_unfinished_last_line_renders_with_every_reader(
    raw_video: Path, log: Path, reader: dict[str, object]
) -> None:
    before = raw_video.read_bytes()
    (log.parent / "activity.jsonl").write_text("")
    # The write stopped inside a multi-byte character.
    log.write_bytes(COMPLETE_RECORDS + b'{"id": "next", "text": "\xe6')
    render(raw_video, log, **reader)
    assert raw_video.read_bytes() != before
    assert frames(raw_video, 0, 0)


# R-LOG-7
@pytest.mark.parametrize("reader", LOG_READERS.values(), ids=LOG_READERS)
@pytest.mark.parametrize(
    "tail",
    [UNFINISHED, b'{"text":"\xe6', COMPLETE_RECORDS.split(b"\n")[-2]],
    ids=["json-fragment", "split-utf8", "json-without-newline"],
)
def test_append_after_interrupted_write_renders_with_every_reader(
    raw_video: Path, log: Path, reader: dict[str, object], tail: bytes
) -> None:
    (log.parent / "activity.jsonl").write_text("")
    log.write_bytes(COMPLETE_RECORDS + tail)
    with log_input(log.parent, "key", {"chord": "Ctrl+s"}) as result:
        result.returncode = 0
    assert log.read_bytes().startswith(COMPLETE_RECORDS)
    render(raw_video, log, **reader)
    assert frames(raw_video, 0, 0)


# R-LOG-8, R-REC-10
@pytest.mark.parametrize("reader", LOG_READERS.values(), ids=LOG_READERS)
def test_append_preserves_complete_corruption_and_render_failure(
    raw_video: Path, log: Path, reader: dict[str, object]
) -> None:
    before = raw_video.read_bytes()
    (log.parent / "activity.jsonl").write_text("")
    corrupt = COMPLETE_RECORDS + b"not json\n"
    log.write_bytes(corrupt + UNFINISHED)
    with log_input(log.parent, "key", {"chord": "Ctrl+s"}):
        pass
    assert log.read_bytes().startswith(corrupt)
    with pytest.raises(RuntimeError) as failure:
        render(raw_video, log, **reader)
    assert_raw_clip_survived(raw_video, before, str(failure.value))

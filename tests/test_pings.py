import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from framewisp.captions import render_recording, subtitle_script
from framewisp.compact import CompactSettings, recording_duration, retained_frames
from framewisp.console import PANEL_WIDTH, ConsoleCapture
from framewisp.pings import PING_SECONDS, Ping, pings_for_clip

SIZE = (1280, 720)
ORIGIN = 100.0
STOPPED = 106.0


def ping_line(time: float, x: int, y: int) -> str:
    return (
        json.dumps(
            {
                "id": "command",
                "event": "ping",
                "time": ORIGIN + time,
                "action": "click",
                "parameters": {},
                "returncode": None,
                "error": None,
                "x": x,
                "y": y,
            }
        )
        + "\n"
    )


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
        size=SIZE,
        **options,  # type: ignore[arg-type]
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
    render(raw_video, log, captions=False)
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
    render(raw_video, log, captions=False)
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
@pytest.mark.parametrize("console", [False, True])
def test_compaction_maps_pings_to_retained_frames(
    raw_video: Path, log: Path, tmp_path: Path, console: bool
) -> None:
    (tmp_path / "console.jsonl").write_text("")
    (tmp_path / "activity.jsonl").write_text("")
    log.write_text(command_lines(4, 4.3) + ping_line(4.25, 600, 300))
    settings = CompactSettings(before=0.2, after=0.1)
    ranges = retained_frames(
        (log, tmp_path / "activity.jsonl"),
        origin=ORIGIN,
        stopped=STOPPED,
        duration=recording_duration(raw_video),
        settings=settings,
        overlays=[(4.25, 4.25 + PING_SECONDS)],
    )
    source = [frame for first, last in ranges for frame in range(first, last)]
    render(
        raw_video,
        log,
        captions=False,
        compact=settings,
        console=ConsoleCapture(tmp_path) if console else None,
    )
    size = (SIZE[0] + (PANEL_WIDTH if console else 0), SIZE[1])
    shown = compacted_ring_frames(raw_video, size=size)
    first = source.index(math.ceil(4.25 * 30))
    # The cut keeps the whole animation, not only its opening frames.
    assert source[first + 14] == source[first] + 14
    assert first <= min(shown) <= first + 1
    assert ring_centre(raw_video, min(shown) + 2, size=size) == pytest.approx(
        (600, 300), abs=1
    )
    assert source[max(shown)] / 30 > 4.25 + 0.3


# R-REC-8
def test_zero_padding_retains_every_ping_frame(raw_video: Path, log: Path) -> None:
    (log.parent / "activity.jsonl").write_text("")
    log.write_text(command_lines(3, 3.01))
    ranges = retained_frames(
        (log, log.parent / "activity.jsonl"),
        origin=ORIGIN,
        stopped=STOPPED,
        duration=recording_duration(raw_video),
        settings=CompactSettings(before=0, after=0),
        overlays=[(3.005, 3.005 + PING_SECONDS)],
    )
    kept = {frame for first, last in ranges for frame in range(first, last)}
    assert set(range(90, 106)) <= kept


# R-REC-8
def test_clip_end_cuts_a_late_ping_short(raw_video: Path, log: Path) -> None:
    (log.parent / "activity.jsonl").write_text("")
    log.write_text(command_lines(5.7, 5.71) + ping_line(5.8, 300, 200))
    render(raw_video, log, captions=False, compact=CompactSettings(before=0, after=0))
    assert len(compacted_ring_frames(raw_video, size=SIZE)) < 15
    assert round(recording_duration(raw_video) * 30) < 180


# R-REC-8
def test_zero_padding_keeps_each_ping_whole(raw_video: Path, log: Path) -> None:
    (log.parent / "activity.jsonl").write_text("")
    log.write_text(command_lines(3, 3.01) + ping_line(3.005, 300, 200))
    render(
        raw_video,
        log,
        captions=False,
        compact=CompactSettings(before=0, after=0),
    )
    shown = compacted_ring_frames(raw_video, size=SIZE)
    # The fading ring outgrows the 28-pixel opening ring only late in its animation.
    assert max(shown.values()) > 60
    assert max(shown) - min(shown) >= 10


# R-REC-9
def test_ring_clips_at_the_console_panel(
    raw_video: Path, log: Path, tmp_path: Path
) -> None:
    (tmp_path / "console.jsonl").write_text("")
    log.write_text(ping_line(1, SIZE[0] - 5, 400) + ping_line(3, 600, 300))
    render(raw_video, log, captions=False, console=ConsoleCapture(tmp_path))
    size = (SIZE[0] + PANEL_WIDTH, SIZE[1])
    edge = frames(raw_video, 36, 36, size=size)[0]
    inside = frames(raw_video, 96, 96, size=size)[0]
    assert red_box(edge.crop((0, 0, SIZE[0], SIZE[1]))) is not None
    assert red_box(edge.crop((SIZE[0], 0, size[0], SIZE[1]))) is None
    box = red_box(inside)
    assert box is not None
    assert (box[0] + box[2]) / 2 == pytest.approx(600, abs=1)
    assert (box[1] + box[3]) / 2 == pytest.approx(300, abs=1)


# R-REC-11
@pytest.mark.parametrize("captions", [False, True])
def test_pings_ignore_the_caption_choice(
    raw_video: Path, log: Path, captions: bool
) -> None:
    log.write_text(command_lines(1, 1.1) + ping_line(1.05, 300, 200))
    render(raw_video, log, captions=captions)
    assert red_box(frames(raw_video, 36, 36)[0]) is not None


# R-REC-7
def test_disabled_pings_leave_the_raw_recording_untouched(
    raw_video: Path, log: Path
) -> None:
    before = raw_video.read_bytes()
    log.write_text(ping_line(1, 300, 200))
    render(raw_video, log, captions=False, click_pings=False)
    assert raw_video.read_bytes() == before


def test_clip_without_presses_is_not_encoded_again(raw_video: Path, log: Path) -> None:
    before = raw_video.read_bytes()
    log.write_text("")
    render(raw_video, log, captions=False)
    assert raw_video.read_bytes() == before


def test_script_has_one_dialogue_per_ping() -> None:
    script = subtitle_script([], pings=[Ping(1, 2, 3), Ping(2, 4, 5)])
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
def test_failed_encoder_leaves_uncaptioned_clip_playable(
    raw_video: Path, log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            render(raw_video, log, captions=False)
    assert "status 3" in str(failure.value)
    assert_raw_clip_survived(raw_video, before, str(failure.value))


# R-REC-10
def test_malformed_log_leaves_uncaptioned_clip_playable(
    raw_video: Path, log: Path
) -> None:
    before = raw_video.read_bytes()
    log.write_text("not json\n")
    with pytest.raises(RuntimeError) as failure:
        render(raw_video, log, captions=False)
    assert_raw_clip_survived(raw_video, before, str(failure.value))

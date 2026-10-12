import json
import subprocess
import time
from pathlib import Path

import pytest

from framewisp.captions import render_recording
from framewisp.compact import (
    CompactSettings,
    activity_events,
    log_activity,
    retained_frames,
)
from framewisp.console import ConsoleCapture


@pytest.fixture
def logs(tmp_path: Path) -> tuple[Path, Path]:
    paths = (tmp_path / "inputs.jsonl", tmp_path / "activity.jsonl")
    for path in paths:
        path.write_text("", encoding="utf-8")
    return paths


def span(log: Path, identifier: str, start: float, end: float | None) -> None:
    with log.open("a", encoding="utf-8") as output:
        for event, timestamp in (("start", start), ("end", end)):
            if timestamp is not None:
                output.write(
                    json.dumps({"id": identifier, "event": event, "time": timestamp})
                    + "\n"
                )


def test_cuts_merge_padding_and_round_outward(logs: tuple[Path, Path]) -> None:
    span(logs[0], "first", 110.01, 111.01)
    span(logs[0], "second", 112, 113)
    span(logs[0], "before-clip", 98, 99)
    span(logs[0], "after-clip", 131, 132)
    assert retained_frames(
        logs, origin=100, stopped=130, duration=30, settings=CompactSettings()
    ) == [(0, 30), (270, 450), (840, 900)]


def test_full_wait_and_inflight_input_survive_clip_boundaries(
    logs: tuple[Path, Path],
) -> None:
    span(logs[1], "ongoing-at-start", 98, 105)
    span(logs[1], "full-wait", 107, 113)
    span(logs[0], "unfinished", 125, None)
    assert retained_frames(
        logs,
        origin=100,
        stopped=130,
        duration=30,
        settings=CompactSettings(before=0, after=0),
    ) == [(0, 150), (210, 390), (750, 900)]


@pytest.mark.parametrize(
    "duration,settings,expected",
    [
        (30, CompactSettings(), [(0, 30), (840, 900)]),
        (30, CompactSettings(before=0, after=0), [(0, 1), (899, 900)]),
        (0.1, CompactSettings(), [(0, 3)]),
        (0, CompactSettings(), [(0, 1)]),
        (30, CompactSettings(before=1e308, after=1e308), [(0, 900)]),
    ],
)
def test_idle_and_short_clips(
    logs: tuple[Path, Path],
    duration: float,
    settings: CompactSettings,
    expected: list[tuple[int, int]],
) -> None:
    assert (
        retained_frames(
            logs, origin=0, stopped=duration, duration=duration, settings=settings
        )
        == expected
    )


def test_partial_record_does_not_end_active_span(logs: tuple[Path, Path]) -> None:
    span(logs[0], "ongoing", 105, None)
    with logs[0].open("ab") as output:
        output.write(b'{"event":"end","text":"\xe6')
    assert retained_frames(
        logs,
        origin=100,
        stopped=110,
        duration=10,
        settings=CompactSettings(before=0, after=0),
    ) == [(0, 1), (150, 300)]


def test_activity_log_closes_cancelled_wait_without_retaining_content(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    with pytest.raises(InterruptedError):
        with log_activity(tmp_path):
            raise InterruptedError("sensitive condition text")
    events = activity_events(tmp_path / "activity.jsonl")
    assert [event["event"] for event in events] == ["start", "end"]
    assert events[0]["id"] == events[1]["id"]
    assert started <= events[0]["time"] <= events[1]["time"] <= time.monotonic()
    assert all(set(event) == {"id", "event", "time"} for event in events)


@pytest.mark.parametrize(
    "raw",
    [
        True,
        {},
        {"before": 1},
        {"before": 1, "after": 2, "extra": 0},
        {"before": True, "after": 2},
        {"before": "1", "after": 2},
        {"before": 10**400, "after": 2},
        {"before": -1, "after": 2},
        {"before": 1, "after": float("inf")},
        {"before": float("nan"), "after": 2},
    ],
)
def test_invalid_wire_settings(raw: object) -> None:
    with pytest.raises(ValueError, match="compact"):
        CompactSettings.parse(raw)


def frame(path: Path, second: float, region: str) -> bytes:
    return subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(second),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"crop={region}",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout


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
            "color=c=black:s=320x240:r=30:d=9",
            "-vf",
            "drawbox=x=0:y=100:w=320:h=140:color=red:t=fill:enable='gte(t,4)'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    return video


@pytest.mark.parametrize("overlays", [False, True])
def test_rendered_cuts_keep_frames_and_align_overlays(
    logs: tuple[Path, Path],
    tmp_path: Path,
    raw_video: Path,
    overlays: bool,
) -> None:
    video = raw_video
    events = [
        {
            "id": "key",
            "action": "key",
            "parameters": {"chord": "Return"},
            "event": event,
            "time": timestamp,
            "returncode": 0,
            "error": None,
        }
        for event, timestamp in (("start", 104), ("end", 104.5))
    ]
    logs[0].write_text("".join(json.dumps(event) + "\n" for event in events))
    (tmp_path / "console.jsonl").write_text(
        json.dumps({"time": 104.2, "text": "Result arrived\n"}) + "\n"
    )
    render_recording(
        video,
        input_log=logs[0],
        origin=100,
        stopped=109,
        log=tmp_path / "render.log",
        captions=overlays,
        console=ConsoleCapture(tmp_path) if overlays else None,
        compact=CompactSettings(before=0.5, after=0.5),
        size=(320, 240),
    )
    summary = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height",
                "-of",
                "json",
                str(video),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        ).stdout
    )
    assert float(summary["format"]["duration"]) == pytest.approx(2.5, abs=1 / 30)
    assert summary["streams"][0]["width"] == (960 if overlays else 320)
    before = frame(video, 0.2, "320:140:0:100")
    after = frame(video, 1.3, "320:140:0:100")
    assert before != after
    final = frame(video, 2.2, "320:140:0:100")
    # H.264 can change individual pixels between otherwise identical frames.
    assert sum(after[::3]) / len(after[::3]) > 240
    assert sum(final[::3]) / len(final[::3]) > 240
    if overlays:
        assert len(set(frame(video, 0.2, "320:50:0:190"))) < 8
        assert len(set(frame(video, 1.3, "320:50:0:190"))) > 30
        assert len(set(frame(video, 2.2, "320:50:0:190"))) < 8
        early_console = frame(video, 0.2, "640:150:320:48")
        assert len(set(early_console)) < 8
        assert len(set(frame(video, 1.3, "640:150:320:48"))) > 30
        assert len(set(frame(video, 2.2, "640:150:320:48"))) > 30


def test_zero_padding_keeps_encoded_end_despite_stop_latency(
    logs: tuple[Path, Path],
    tmp_path: Path,
    raw_video: Path,
) -> None:
    render_recording(
        raw_video,
        input_log=logs[0],
        origin=100,
        stopped=110,
        log=tmp_path / "render.log",
        captions=False,
        compact=CompactSettings(before=0, after=0),
        size=(320, 240),
    )
    duration = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(raw_video),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=5,
    ).stdout
    assert float(duration) == pytest.approx(2 / 30, abs=0.001)
    assert sum(frame(raw_video, 0, "320:140:0:100")) == 0
    final = frame(raw_video, 0.025, "320:140:0:100")
    assert sum(final[::3]) / len(final[::3]) > 240

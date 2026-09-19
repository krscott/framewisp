import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image, ImageChops


@dataclass(frozen=True)
class Demo:
    directory: Path
    process: subprocess.Popen[bytes]
    runtime: Path
    child_pids: list[int]
    recording: Path | None


def wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "Demo did not reach the expected state"
        time.sleep(0.05)


def cli(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["framewisp", "--session", str(directory), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert (
        result.returncode == 0
    ), f"{arguments}: {result.stdout}\n{result.stderr}\n" + "\n".join(
        f"{log.name}:\n{log.read_text()}" for log in directory.glob("*.log")
    )
    return result


@pytest.fixture
def demo(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Demo]:
    directory = tmp_path / "session"
    runner_log = tmp_path / "runner.log"
    mode = getattr(request, "param", None)
    recording = tmp_path / "session.mp4" if mode is True else None
    command = ["framewisp", "--session", str(directory), "run"]
    if recording is not None:
        command.extend(["--record", str(recording)])
    app = (
        [sys.executable, str(Path(__file__).with_name("input_probe.py"))]
        if mode == "probe"
        else ["framewisp-demo"]
    )
    command.extend(["--", *app])
    with runner_log.open("w") as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:

        def ready() -> bool:
            assert process.poll() is None, runner_log.read_text()
            return "Session ready:" in runner_log.read_text()

        wait_until(ready)
        state = json.loads((directory / "session.json").read_text())
        yield Demo(
            directory=directory,
            process=process,
            runtime=Path(state["runtime_directory"]),
            child_pids=list(state["processes"].values()),
            recording=recording,
        )
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=20)


@pytest.mark.integration
def test_agent_can_see_type_and_click(demo: Demo, tmp_path: Path) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"

    def app_is_visible() -> bool:
        cli(demo.directory, "screenshot", str(before))
        with Image.open(before) as image:
            assert image.format == "PNG"
            assert image.size == (1280, 720)
            # The label contains text once the app has painted, unlike the empty output.
            return len(image.crop((40, 210, 440, 240)).getcolors() or []) > 1

    wait_until(app_is_visible)
    with Image.open(before) as image:
        original_entry = image.crop((50, 85, 400, 115))
        original_label = image.crop((40, 210, 440, 240))

    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "Hello Wayland!")
    cli(demo.directory, "key", "BackSpace")
    cli(demo.directory, "key", "Return")
    wait_until(
        lambda: "Entered: Hello Wayland\n" in (demo.directory / "app.log").read_text()
    )
    cli(demo.directory, "click", "120", "170")
    wait_until(
        lambda: "Applied: Hello Wayland\n" in (demo.directory / "app.log").read_text()
    )

    def result_is_visible() -> bool:
        cli(demo.directory, "screenshot", str(after))
        with Image.open(after) as image:
            entry = image.crop((50, 85, 400, 115))
            label = image.crop((40, 210, 440, 240))
        return (
            ImageChops.difference(original_entry, entry).getbbox() is not None
            and ImageChops.difference(original_label, label).getbbox() is not None
        )

    wait_until(result_is_visible)
    # Tab focuses the entry and GTK selects its text, so typing replaces it.
    cli(demo.directory, "key", "Tab")
    cli(demo.directory, "type", "?")
    cli(demo.directory, "key", "Return")
    wait_until(lambda: "Entered: ?\n" in (demo.directory / "app.log").read_text())

    # Drag across the entry to select its text, then replace the selection.
    cli(demo.directory, "type", "abcdef")
    cli(demo.directory, "drag", "400", "100", "50", "100")
    cli(demo.directory, "type", "dragged")
    cli(demo.directory, "click", "120", "170")
    wait_until(lambda: "Applied: dragged\n" in (demo.directory / "app.log").read_text())

    demo.process.send_signal(signal.SIGTERM)
    assert demo.process.wait(timeout=20) == 0
    assert not (demo.directory / "session.json").exists()
    assert not demo.runtime.exists()
    for pid in demo.child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.integration
def test_screenshot_waits_before_capture(demo: Demo, tmp_path: Path) -> None:
    destination = tmp_path / "delayed.png"
    started = time.time()
    cli(demo.directory, "screenshot", "--delay", "0.5", str(destination))
    # Check the file's write time so sleeping after capture would fail this test.
    assert destination.stat().st_mtime >= started + 0.5
    with Image.open(destination) as image:
        assert image.format == "PNG"
        assert image.size == (1280, 720)


@pytest.mark.integration
def test_interrupt_stops_session(demo: Demo) -> None:
    demo.process.send_signal(signal.SIGINT)
    assert demo.process.wait(timeout=20) == 0
    assert not demo.runtime.exists()
    for pid in demo.child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def recording_frames(path: Path) -> set[str]:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    streams = json.loads(probe.stdout)["streams"]
    assert len(streams) == 1
    assert streams[0]["codec_name"] == "h264"
    assert (streams[0]["width"], streams[0]["height"]) == (1280, 720)
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    frames = {
        line.rsplit(",", 1)[1].strip()
        for line in decoded.stdout.splitlines()
        if line and not line.startswith("#")
    }
    assert frames
    return frames


@pytest.mark.integration
@pytest.mark.parametrize("demo", [True], indirect=True)
@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_recording_finalizes_on_shutdown(
    demo: Demo, stop_signal: signal.Signals
) -> None:
    assert demo.recording is not None
    screenshot = demo.directory / "visible.png"

    def app_is_visible() -> bool:
        cli(demo.directory, "screenshot", str(screenshot))
        with Image.open(screenshot) as image:
            return len(image.crop((40, 210, 440, 240)).getcolors() or []) > 1

    wait_until(app_is_visible)
    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "recorded")
    cli(demo.directory, "key", "Return")
    cli(demo.directory, "screenshot", "--delay", "0.2", str(screenshot))
    demo.process.send_signal(stop_signal)
    assert demo.process.wait(timeout=20) == 0
    assert len(recording_frames(demo.recording)) > 1
    # Check a uniform background patch against the screenshot. A range mismatch
    # can produce a playable video with visibly shifted brightness.
    with Image.open(screenshot) as image:
        expected = image.convert("RGB").crop((1000, 600, 1002, 602)).tobytes()
    samples = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(demo.recording),
            "-vf",
            "fps=10,crop=2:2:1000:600",
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
    assert any(
        max(
            abs(a - b)
            for a, b in zip(samples[offset : offset + 12], expected, strict=True)
        )
        <= 3
        for offset in range(0, len(samples), 12)
    )
    assert not demo.runtime.exists()
    assert not (demo.directory / "session.json").exists()
    for pid in demo.child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.integration
def test_recording_starts_before_app_and_finalizes_on_app_exit(tmp_path: Path) -> None:
    video = tmp_path / "quick.mp4"
    result = subprocess.run(
        [
            "framewisp",
            "--session",
            str(tmp_path / "session"),
            "run",
            "--record",
            str(video),
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; assert Path(sys.argv[1]).stat().st_size > 0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    recording_frames(video)


@pytest.mark.integration
@pytest.mark.parametrize("demo", [True], indirect=True)
def test_recorder_failure_stops_session(demo: Demo) -> None:
    state = json.loads((demo.directory / "session.json").read_text())
    os.kill(state["processes"]["recorder"], signal.SIGKILL)
    assert demo.process.wait(timeout=20) != 0
    assert "recorder.log" in (demo.directory.parent / "runner.log").read_text()
    assert not demo.runtime.exists()
    for pid in demo.child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.integration
def test_recording_startup_failure_does_not_launch_app(tmp_path: Path) -> None:
    session = tmp_path / "session"
    marker = tmp_path / "app-started"
    result = subprocess.run(
        [
            "framewisp",
            "--session",
            str(session),
            "run",
            "--record",
            str(tmp_path / "missing" / "capture.mp4"),
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            str(marker),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode != 0
    assert "recorder.log" in result.stderr
    assert not marker.exists()
    assert not (session / "session.json").exists()


def input_events(demo: Demo) -> list[dict[str, str | float | bool]]:
    return [
        json.loads(line)
        for line in (demo.directory / "app.log").read_text().splitlines()
        if line.startswith('{"event":')
    ]


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
@pytest.mark.parametrize("duration", [None, 0, 0.6])
def test_drag_delivers_paced_motion_and_release(
    demo: Demo, duration: float | None
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    options = [] if duration is None else ["--duration", str(duration)]
    cli(demo.directory, "drag", *options, "100", "100", "500", "300")
    wait_until(lambda: any(event["event"] == "release" for event in input_events(demo)))
    events = input_events(demo)
    press = next(event for event in events if event["event"] == "press")
    release = next(event for event in events if event["event"] == "release")
    assert (press["x"], press["y"]) == (100, 100)
    assert (release["x"], release["y"]) == (500, 300)
    expected_duration = 0.4 if duration is None else duration
    elapsed = float(release["time"]) - float(press["time"])
    assert expected_duration * 0.9 <= elapsed < expected_duration + 2
    motion = [
        event for event in events if event["event"] == "motion" and event["pressed"]
    ]
    if expected_duration:
        intermediate = [event for event in motion if 100 < float(event["x"]) < 500]
        assert len(intermediate) >= 3
        for event in intermediate:
            # The path is straight, within integer-coordinate rounding.
            assert abs(float(event["y"]) - (100 + (float(event["x"]) - 100) / 2)) <= 1


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
@pytest.mark.parametrize("interval,text", [(None, "Ab c"), (0, "Ab c"), (5.5, "Ab c")])
def test_typing_paces_received_characters(
    demo: Demo, interval: float | None, text: str
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "click", "100", "425")
    options = [] if interval is None else ["--interval", str(interval)]
    cli(demo.directory, "type", *options, text)
    finished = time.monotonic()
    wait_until(lambda: any(event.get("text") == text for event in input_events(demo)))
    events = [event for event in input_events(demo) if event["event"] == "text"]
    assert [event["text"] for event in events] == [
        text[:end] for end in range(1, len(text) + 1)
    ]
    expected_interval = 0.08 if interval is None else interval
    for previous, current in zip(events, events[1:]):
        elapsed = float(current["time"]) - float(previous["time"])
        assert expected_interval * 0.9 <= elapsed < expected_interval + 2
    # A 16.5-second action exceeds both original deadlines. It must also return
    # without sleeping for another 5.5 seconds after the final character.
    assert finished - float(events[-1]["time"]) < 3

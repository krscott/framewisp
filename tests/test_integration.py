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
    return subprocess.run(
        ["framewisp", "--session", str(directory), *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )


@pytest.fixture
def demo(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Demo]:
    directory = tmp_path / "session"
    runner_log = tmp_path / "runner.log"
    recording = tmp_path / "session.mp4" if getattr(request, "param", False) else None
    command = ["framewisp", "--session", str(directory), "run"]
    if recording is not None:
        command.extend(["--record", str(recording)])
    command.extend(["--", "framewisp-demo"])
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

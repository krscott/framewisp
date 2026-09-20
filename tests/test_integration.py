import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        ["framewisp", str(directory), *arguments],
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
    directory = tmp_path / "session with spaces"
    runner_log = tmp_path / "runner.log"
    mode = getattr(request, "param", None)
    x11 = isinstance(mode, str) and mode.startswith("x11")
    if x11:
        assert isinstance(mode, str)
        mode = {
            "x11": None,
            "x11-probe": "probe",
            "x11-record": True,
            "x11-clipboard": "clipboard",
            "x11-qt": "qt",
            "x11-waits": "waits",
        }[mode]
    recording = (
        tmp_path / "session.mp4"
        if mode is True or mode in {"large", "uncaptioned"}
        else None
    )
    command = ["framewisp", str(directory), "run"]
    if x11:
        command.append("--x11")
    if recording is not None:
        command.extend(["--record", str(recording)])
        if mode == "uncaptioned":
            command.append("--no-captions")
    if mode in {"large", "odd"}:
        width, height = (1600, 900) if mode == "large" else (1601, 901)
        command.extend(["--width", str(width), "--height", str(height)])
    probes = {
        "probe": "input_probe.py",
        "waits": "wait_probe.py",
        "clipboard": "input_probe.py",
        "scroll": "scroll_probe.py",
        "large": "input_probe.py",
        "odd": "input_probe.py",
    }
    app = (
        [sys.executable, str(Path(__file__).with_name(probes[mode]))]
        if mode in probes
        else ["framewisp-demo"]
    )
    if mode == "qt":
        app = ["qml", str(Path(__file__).with_name("menu_probe.qml"))]
    command.extend(["--", *app])
    with runner_log.open("w") as output:
        process = subprocess.Popen(
            command,
            env=os.environ
            | (
                {
                    "QT_QUICK_BACKEND": "software",
                    "QT_QPA_PLATFORMTHEME": "",
                    "QT_IM_MODULE": "",
                    "QT_LOGGING_RULES": "qml.debug=true",
                    "QT_LOGGING_TO_CONSOLE": "1",
                }
                if mode == "qt"
                else {}
            )
            | ({"WAYLAND_DEBUG": "client"} if mode == "clipboard" else {})
            | (
                {
                    "DISPLAY": ":99999",
                    "WAYLAND_DISPLAY": "host-display-do-not-use",
                    "XAUTHORITY": str(tmp_path / "host-authority"),
                }
                if x11
                else {}
            ),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    x11_children: list[int] = []
    try:

        def ready() -> bool:
            assert process.poll() is None, runner_log.read_text()
            return "Session ready:" in runner_log.read_text()

        wait_until(ready)
        state = json.loads((directory / "session.json").read_text())
        if x11:
            # wlroots double-forks Xwayland, so it is not a Sway child in /proc.
            for path in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    argv = path.read_bytes().split(b"\0")
                except OSError:
                    continue  # Other system processes can exit during enumeration.
                if b"Xwayland" in argv[0] and state["x11_display"].encode() in argv:
                    x11_children.append(int(path.parent.name))
            assert len(x11_children) == 1
            x11_socket = Path("/tmp/.X11-unix") / f"X{state['x11_display'][1:]}"
            assert x11_socket.is_socket()
            assert state["x11_display"] != ":99999"
            app_env = (
                Path(f"/proc/{state['processes']['app']}/environ")
                .read_bytes()
                .split(b"\0")
            )
            assert b"WAYLAND_DISPLAY=host-display-do-not-use" not in app_env
            assert b"XAUTHORITY=/dev/null" in app_env
            assert not any(item.startswith(b"WAYLAND_DISPLAY=") for item in app_env)
            if mode not in {"probe", "clipboard", "qt", "waits"}:
                wait_until(
                    lambda: "Display: X11Display" in (directory / "app.log").read_text()
                )
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
        for pid in x11_children:
            wait_until(lambda: not Path(f"/proc/{pid}").exists())


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
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
    # Shift+Tab moves back from Apply to the entry and selects its text.
    cli(demo.directory, "key", "Shift+Tab")
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


@pytest.mark.integration
@pytest.mark.parametrize("demo", [True, "x11-record"], indirect=True)
def test_status_and_stop_finalize_recording(demo: Demo) -> None:
    assert demo.recording is not None
    state = json.loads(cli(demo.directory, "status").stdout)
    assert state["status"] == "running"
    assert state["backend"] in {"wayland", "x11"}
    assert state["app"]["running"]
    assert state["recording"]["running"]
    assert state["recording"]["path"] == str(demo.recording)
    cli(demo.directory, "type", "stop test")
    stopped = json.loads(cli(demo.directory, "stop").stdout)
    assert stopped["status"] == "stopped"
    summary = stopped["recording"]
    assert summary["path"] == str(demo.recording)
    assert summary["width"] == 1280 and summary["height"] == 720
    assert summary["duration_seconds"] > 0
    assert summary["size_bytes"] == demo.recording.stat().st_size
    # The reply comes after cleanup, rather than merely acknowledging the request.
    assert not demo.runtime.exists()
    assert not (demo.directory / "session.json").exists()
    for pid in demo.child_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert demo.process.wait(timeout=5) == 0
    recording_frames(demo.recording)
    log = (demo.directory / "recorder.log").read_text()
    assert len(re.findall(r"zwlr_screencopy_frame_v1[#@]\d+\.ready", log)) == 1
    assert "get_registry" not in log
    assert "discarded wl_buffer" not in log
    assert "libx264" in log


@pytest.mark.integration
def test_status_and_stop_reject_copied_session_metadata(
    demo: Demo, tmp_path: Path
) -> None:
    other = tmp_path / "copied-session"
    other.mkdir()
    (other / "session.json").write_bytes((demo.directory / "session.json").read_bytes())
    for action in ("status", "stop"):
        result = subprocess.run(
            ["framewisp", str(other), action],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        assert result.returncode == 1
        assert "different session" in result.stderr
        assert demo.process.poll() is None
    assert json.loads(cli(demo.directory, "status").stdout)["recording"] is None


@pytest.mark.integration
@pytest.mark.parametrize("demo", [True], indirect=True)
def test_stop_reports_recording_finalization_failure(demo: Demo) -> None:
    cli(demo.directory, "type", "caption this")
    (demo.directory / "captions.log").mkdir()
    result = subprocess.run(
        ["framewisp", str(demo.directory), "stop"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert "captions.log" in result.stderr
    assert result.stdout == ""
    assert demo.process.wait(timeout=5) == 1
    assert not demo.runtime.exists()
    assert not (demo.directory / "session.json").exists()
    assert demo.recording is not None
    recording_frames(demo.recording)


@pytest.mark.integration
def test_disconnected_display_error_has_context(tmp_path: Path) -> None:
    session = tmp_path / "disconnected"
    session.mkdir()
    (session / "session.json").write_text(
        json.dumps(
            {
                "control_protocol": 1,
                "runtime_directory": str(tmp_path),
                "wayland_display": "wayland-does-not-exist",
            }
        )
    )
    for arguments in (
        ("screenshot", str(tmp_path / "out.png")),
        ("status",),
        ("stop",),
    ):
        result = subprocess.run(
            ["framewisp", str(session), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 1
        assert str(session) in result.stderr
        assert str(tmp_path) in result.stderr
        assert "sandbox" in result.stderr
        assert "Traceback" not in result.stderr


def recording_frames(path: Path, *, size: tuple[int, int] = (1280, 720)) -> set[str]:
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
    assert (streams[0]["width"], streams[0]["height"]) == size
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
@pytest.mark.parametrize("demo", [True, "uncaptioned"], indirect=True)
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
def test_move_shows_tooltip_without_clicking(demo: Demo, tmp_path: Path) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    before, hovered, after = [
        tmp_path / name for name in ["before.png", "hovered.png", "after.png"]
    ]
    cli(demo.directory, "move", "100", "600")
    cli(demo.directory, "screenshot", "--delay", "0.5", str(before))
    cli(demo.directory, "move", "500", "200")
    cli(demo.directory, "screenshot", "--delay", "1", str(hovered))
    events = input_events(demo)
    assert any(
        event["event"] == "motion"
        and event["x"] == 500
        and event["y"] == 200
        and not event["pressed"]
        for event in events
    )
    assert any(
        event["event"] == "tooltip" and not event["keyboard"] for event in events
    )
    assert not any(event["event"] in {"press", "release"} for event in events)
    cli(demo.directory, "move", "100", "600")
    cli(demo.directory, "screenshot", "--delay", "0.2", str(after))
    with (
        Image.open(before) as original,
        Image.open(hovered) as tooltip,
        Image.open(after) as restored,
    ):
        # The drawing area excludes the entry's blinking text cursor.
        region = (0, 0, 1280, 400)
        assert (
            ImageChops.difference(
                original.crop(region).convert("RGB"),
                tooltip.crop(region).convert("RGB"),
            ).getbbox()
            is not None
        )
        assert (
            ImageChops.difference(
                original.crop(region).convert("RGB"),
                restored.crop(region).convert("RGB"),
            ).getbbox()
            is None
        )


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe", "x11-probe"], indirect=True)
@pytest.mark.parametrize("button", [None, "right"])
@pytest.mark.parametrize("count", [None, 2])
def test_click_delivers_button_and_recognized_count(
    demo: Demo, button: str | None, count: int | None
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    options = [] if button is None else ["--button", button]
    if count is not None:
        options.extend(["--count", str(count)])
    cli(demo.directory, "click", *options, "200", "150")
    expected_count = count or 1
    wait_until(
        lambda: sum(event["event"] == "release" for event in input_events(demo))
        == expected_count
    )
    events = [
        event for event in input_events(demo) if event["event"] in {"press", "release"}
    ]
    assert [event["event"] for event in events] == ["press", "release"] * expected_count
    assert [event["count"] for event in events] == [
        n for n in range(1, expected_count + 1) for _ in range(2)
    ]
    for event in events:
        assert event["button"] == (3 if button == "right" else 1)
        assert (event["x"], event["y"]) == (200, 150)


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
@pytest.mark.parametrize(
    "action,button,modifiers",
    [
        ("click", "left", ("shift",)),
        ("click", "right", ("ctrl", "shift")),
        ("drag", "left", ("ctrl",)),
        ("drag", "right", ("alt",)),
        ("drag", "right", ("ctrl", "shift", "alt")),
    ],
)
def test_pointer_gesture_holds_and_releases_modifiers(
    demo: Demo, action: str, button: str, modifiers: tuple[str, ...]
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    options = ["--button", button]
    for modifier in modifiers:
        options.extend(["--modifier", modifier.upper()])
    coordinates = ["200", "150"] if action == "click" else ["100", "100", "500", "300"]
    cli(demo.directory, action, *options, *coordinates)
    wait_until(lambda: any(event["event"] == "release" for event in input_events(demo)))
    events = input_events(demo)
    buttons = [event for event in events if event["event"] in {"press", "release"}]
    assert [event["event"] for event in buttons] == ["press", "release"]
    for event in buttons:
        assert event["button"] == (1 if button == "left" else 3)
        for modifier in ("ctrl", "shift", "alt"):
            assert event[modifier] == (modifier in modifiers)
    names = {"ctrl": "Control_L", "shift": "Shift_L", "alt": "Alt_L"}
    assert [event["key"] for event in events if event["event"] == "key-press"] == [
        names[modifier] for modifier in modifiers
    ]
    assert [event["key"] for event in events if event["event"] == "key-release"] == [
        names[modifier] for modifier in reversed(modifiers)
    ]
    if action == "drag":
        motion = [
            event for event in events if event["event"] == "motion" and event["pressed"]
        ]
        assert len(motion) > 2
        for event in motion:
            assert event["right"] == (button == "right")
            for modifier in ("ctrl", "shift", "alt"):
                assert event[modifier] == (modifier in modifiers)
        assert (buttons[-1]["x"], buttons[-1]["y"]) == (500, 300)
    cli(demo.directory, "click", "600", "100")
    wait_until(
        lambda: sum(event["event"] == "release" for event in input_events(demo)) == 2
    )
    plain = [event for event in input_events(demo) if event["event"] == "press"][-1]
    assert plain["button"] == 1
    assert not any(plain[modifier] for modifier in ("ctrl", "shift", "alt"))


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
def test_input_devices_remain_between_commands(demo: Demo) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    ready = next(event for event in input_events(demo) if event["event"] == "ready")
    assert ready["pointer"] and ready["keyboard"]
    cli(demo.directory, "click", "--button", "right", "200", "150")
    cli(demo.directory, "key", "a")
    wait_until(
        lambda: any(event["event"] == "key-release" for event in input_events(demo))
    )
    cli(
        demo.directory,
        "screenshot",
        "--delay",
        "0.2",
        str(demo.directory / "after.png"),
    )
    assert not any(event["event"] == "device-removed" for event in input_events(demo))


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe", "x11-probe"], indirect=True)
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
@pytest.mark.parametrize("demo", ["probe", "x11-probe"], indirect=True)
@pytest.mark.parametrize("interval", [None, 0, 5.5])
@pytest.mark.parametrize("text", ["Ab c", "é中🙂a"])
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
    # Check key receipt separately from text updates. Both input tools send zero
    # protocol timestamps, so allow 30 ms of observed GTK callback jitter.
    keys = [event for event in input_events(demo) if event["event"] == "key-press"]
    assert len(keys) == len(text)
    for previous, current in zip(keys, keys[1:]):
        elapsed = float(current["time"]) - float(previous["time"])
        assert max(0, expected_interval * 0.9 - 0.03) <= elapsed < expected_interval + 2
    # A 16.5-second action exceeds both original deadlines. It must also return
    # without sleeping for another 5.5 seconds after the final character.
    assert finished - float(events[-1]["time"]) < 3


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_shortcuts_select_and_edit_text(demo: Demo) -> None:
    capture = demo.directory / "ready.png"

    def entry_is_visible() -> bool:
        cli(demo.directory, "screenshot", str(capture))
        with Image.open(capture) as image:
            return len(image.crop((40, 210, 440, 240)).getcolors() or []) > 1

    wait_until(entry_is_visible)
    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "abcd")
    for chord in ["Left", "Left", "Delete", "BackSpace", "Shift+Right"]:
        cli(demo.directory, "key", chord)
    cli(demo.directory, "type", "Z")
    cli(demo.directory, "key", "Return")
    wait_until(lambda: "Entered: aZ\n" in (demo.directory / "app.log").read_text())
    cli(demo.directory, "key", "Ctrl+A")
    cli(demo.directory, "type", "replaced")
    cli(demo.directory, "key", "Return")
    wait_until(
        lambda: "Entered: replaced\n" in (demo.directory / "app.log").read_text()
    )


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
@pytest.mark.parametrize(
    "chord,key,modifiers",
    [
        ("Ctrl+Shift+z", "Z", ["Control_L", "Shift_L"]),
        ("Alt+x", "x", ["Alt_L"]),
        ("Shift+a", "A", ["Shift_L"]),
        ("Shift+1", "exclam", ["Shift_L"]),
        ("Shift+Tab", "ISO_Left_Tab", ["Shift_L"]),
        ("Escape", "Escape", []),
        ("Up", "Up", []),
        ("Down", "Down", []),
        ("Space", "space", []),
        ("7", "7", []),
    ],
)
def test_key_combination_events(
    demo: Demo, chord: str, key: str, modifiers: list[str]
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "click", "100", "425")
    cli(demo.directory, "key", chord)
    expected = [
        *(("key-press", modifier) for modifier in modifiers),
        ("key-press", key),
        ("key-release", key),
        *(("key-release", modifier) for modifier in reversed(modifiers)),
    ]

    def keyboard_events() -> list[dict[str, str | float | bool]]:
        return [
            event
            for event in input_events(demo)
            if str(event["event"]).startswith("key-")
        ]

    wait_until(lambda: len(keyboard_events()) >= len(expected))
    events = keyboard_events()
    assert [(event["event"], event["key"]) for event in events] == expected
    key_press = events[len(modifiers)]
    assert key_press["ctrl"] == ("Control_L" in modifiers)
    assert key_press["shift"] == ("Shift_L" in modifiers)
    assert key_press["alt"] == ("Alt_L" in modifiers)
    # A following unmodified key must arrive with no modifiers still held.
    cli(demo.directory, "key", "q")
    wait_until(lambda: len(keyboard_events()) >= len(expected) + 2)
    final_press = keyboard_events()[-2]
    assert final_press["key"] == "q"
    assert not any(final_press[name] for name in ["ctrl", "shift", "alt"])


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["scroll"], indirect=True)
@pytest.mark.parametrize(
    "direction,reverse,axis", [("down", "up", "y"), ("right", "left", "x")]
)
def test_scroll_targets_pane_and_returns_to_start(
    demo: Demo, direction: str, reverse: str, axis: str
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "scroll", "900", "300", direction, "--steps", "3")

    def position(pane: str) -> float:
        events = [
            event
            for event in input_events(demo)
            if event["event"] == "position"
            and event["pane"] == pane
            and event["axis"] == axis
        ]
        return float(events[-1]["value"]) if events else 0.0

    wait_until(lambda: position("right") > 0)
    assert position("left") == 0
    events = [event for event in input_events(demo) if event["event"] == "scroll"]
    assert all(event["pane"] == "right" for event in events)
    assert sum(float(event[f"d{axis}"]) for event in events) == 3
    cli(demo.directory, "scroll", "900", "300", reverse, "--steps", "3")
    wait_until(lambda: position("right") == 0)
    # Move to the other pane and use the default one-step count.
    cli(demo.directory, "scroll", "300", "300", direction)
    wait_until(lambda: position("left") > 0)
    events = [
        event
        for event in input_events(demo)
        if event["event"] == "scroll" and event["pane"] == "left"
    ]
    assert sum(float(event[f"d{axis}"]) for event in events) == 1
    assert position("right") == 0


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["clipboard", "x11-clipboard"], indirect=True)
def test_clipboard_copy_paste_survives_input_connections(demo: Demo) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "click", "100", "425")
    for text in ["one", "two", "three"]:
        cli(demo.directory, "key", "Ctrl+a")
        cli(demo.directory, "type", "--interval", "0", text)
        for chord in ["Ctrl+a", "Ctrl+c"]:
            cli(demo.directory, "key", chord)
        # Tear down clients while the app owns a clipboard selection. Each
        # client also tries to overwrite it through VNC clipboard forwarding.
        for _ in range(100):
            disconnect_vnc_with_clipboard(demo.runtime)
        for chord in ["Right", "Ctrl+v"]:
            cli(demo.directory, "key", chord)
        wait_until(
            lambda: any(event.get("text") == text * 2 for event in input_events(demo))
        )
    protocol_log = (demo.directory / "wayvnc.log").read_text()
    assert "create_virtual_keyboard(" in protocol_log
    assert "get_data_device(" not in protocol_log
    assert demo.process.poll() is None


def disconnect_vnc_with_clipboard(runtime: Path) -> None:
    """Complete an RFB 3.8 handshake, send clipboard text, and disconnect."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(runtime / "vnc.sock"))

        def receive(count: int) -> bytes:
            data = b""
            while len(data) < count:
                chunk = connection.recv(count - len(data))
                assert chunk, "VNC server disconnected during handshake"
                data += chunk
            return data

        assert receive(12) == b"RFB 003.008\n"
        connection.sendall(b"RFB 003.008\n")
        assert 1 in receive(receive(1)[0])  # Security type None.
        connection.sendall(b"\x01")
        assert receive(4) == bytes(4)  # SecurityResult success.
        connection.sendall(b"\x01")  # ClientInit: share the display.
        server_init = receive(24)
        receive(struct.unpack("!I", server_init[20:24])[0])
        text = b"remote clipboard must not replace application text"
        connection.sendall(struct.pack("!B3xI", 6, len(text)) + text)


@pytest.mark.integration
@pytest.mark.parametrize(
    "demo,size", [("large", (1600, 900)), ("odd", (1601, 901))], indirect=["demo"]
)
def test_custom_display_size(demo: Demo, size: tuple[int, int], tmp_path: Path) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    capture = tmp_path / "custom-size.png"
    cli(demo.directory, "screenshot", str(capture))
    with Image.open(capture) as image:
        assert image.size == size
    # This point is outside the default display. Check coordinates received by GTK.
    cli(demo.directory, "click", "1500", "300")
    wait_until(lambda: any(event["event"] == "release" for event in input_events(demo)))
    release = next(event for event in input_events(demo) if event["event"] == "release")
    assert (release["x"], release["y"]) == (1500, 300)
    cli(demo.directory, "click", "100", "425")
    cli(demo.directory, "type", "larger display")
    wait_until(
        lambda: any(
            event.get("text") == "larger display" for event in input_events(demo)
        )
    )
    demo.process.terminate()
    assert demo.process.wait(timeout=20) == 0
    if demo.recording is not None:
        assert len(recording_frames(demo.recording, size=size)) > 1


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe", "x11-probe"], indirect=True)
def test_unicode_text_and_later_ascii_input(demo: Demo) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "click", "100", "425")
    text = "-café Ελληνικά Русский 日本語 😀 e\u0301"
    cli(demo.directory, "type", "--interval", "0", "--", text)
    wait_until(lambda: any(event.get("text") == text for event in input_events(demo)))
    cli(demo.directory, "type", " ASCII")
    wait_until(
        lambda: any(
            event.get("text") == text + " ASCII" for event in input_events(demo)
        )
    )
    cli(demo.directory, "key", "Ctrl+a")
    cli(demo.directory, "type", "replaced")
    wait_until(
        lambda: any(event.get("text") == "replaced" for event in input_events(demo))
    )


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, True, "x11-record"], indirect=True)
def test_record_multiple_clips_without_restarting_app(
    demo: Demo, tmp_path: Path
) -> None:
    state_path = demo.directory / "session.json"
    app_pid = json.loads(state_path.read_text())["processes"]["app"]
    if demo.recording is not None:
        cli(demo.directory, "record-stop")
        recording_frames(demo.recording)

    def rejected(*arguments: str) -> str:
        result = subprocess.run(
            ["framewisp", str(demo.directory), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert result.returncode == 1, result.stderr
        assert demo.process.poll() is None
        return result.stderr

    assert "No recording" in rejected("record-stop")
    assert "recorder.log" in rejected(
        "record-start", str(tmp_path / "missing" / "bad.mp4")
    )
    assert "reserved" in rejected("record-start", str(state_path))
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"keep")
    assert "already exists" in rejected("record-start", str(existing))
    assert existing.read_bytes() == b"keep"
    for index in range(2):
        cli(demo.directory, "click", "120", "100")
        cli(demo.directory, "key", "Ctrl+a")
        cli(demo.directory, "type", f"Setup {index}")
        clip = tmp_path / f"clip {index}.mp4"
        cli(demo.directory, "record-start", str(clip))
        assert clip.stat().st_size > 0
        assert "recorder" in json.loads(state_path.read_text())["processes"]
        refused = tmp_path / "refused.mp4"
        assert "already active" in rejected("record-start", str(refused))
        assert not refused.exists()
        cli(demo.directory, "type", " recording")
        cli(demo.directory, "key", "Return")
        cli(
            demo.directory, "screenshot", "--delay", "0.2", str(tmp_path / "during.png")
        )
        summary = json.loads(cli(demo.directory, "record-stop").stdout)
        assert summary["path"] == str(clip)
        assert summary["width"] == 1280 and summary["height"] == 720
        assert summary["duration_seconds"] > 0
        assert summary["size_bytes"] == clip.stat().st_size
        assert len(recording_frames(clip)) > 1
        state = json.loads(state_path.read_text())
        assert state["processes"]["app"] == app_pid
        assert "recorder" not in state["processes"]
        assert demo.process.poll() is None
        cli(demo.directory, "screenshot", str(tmp_path / "between.png"))

    final_clip = tmp_path / "shutdown.mp4"
    cli(demo.directory, "record-start", str(final_clip))
    demo.process.terminate()
    assert demo.process.wait(timeout=20) == 0
    recording_frames(final_clip)


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["odd"], indirect=True)
def test_record_start_rejects_odd_display(demo: Demo, tmp_path: Path) -> None:
    destination = tmp_path / "odd.mp4"
    result = subprocess.run(
        ["framewisp", str(demo.directory), "record-start", str(destination)],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert "even" in result.stderr
    assert not destination.exists()
    assert demo.process.poll() is None


def video_patch(path: Path, second: float) -> bytes:
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
            "crop=600:64:340:644",
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


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_captioned_clips_and_opt_out(demo: Demo, tmp_path: Path) -> None:
    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "Setup outside the clip")
    cli(demo.directory, "key", "Ctrl+a")
    cli(demo.directory, "screenshot", str(tmp_path / "before.png"))
    # This region is below the demo controls and remains unchanged by input.
    with Image.open(tmp_path / "before.png") as screenshot:
        patch = screenshot.convert("RGB").crop((340, 644, 940, 708)).tobytes()
    for captions in [True, True, False]:
        index = len(list(tmp_path.glob("clip-*.mp4")))
        clip = tmp_path / f"clip-{index}.mp4"
        cli(
            demo.directory,
            "record-start",
            str(clip),
            *([] if captions else ["--no-captions"]),
        )
        cli(demo.directory, "screenshot", "--delay", "0.2", str(tmp_path / "idle.png"))
        cli(demo.directory, "type", "--interval", "0.12", "café 日本語 😀")
        cli(demo.directory, "key", "Return")
        cli(demo.directory, "screenshot", "--delay", "1", str(tmp_path / "after.png"))
        cli(demo.directory, "record-stop")
        assert demo.process.poll() is None
        events = [
            json.loads(line)
            for line in (demo.directory / "inputs.jsonl").read_text().splitlines()
        ]
        assert all(
            event["returncode"] == 0 for event in events if event["event"] == "end"
        )
        if captions:
            # Use the recorder's captured-frame timestamp, not the command's launch time.
            timestamp = re.search(
                r"\.ready\((\d+), (\d+), (\d+)\)",
                (demo.directory / "recorder.log").read_text(),
            )
            assert timestamp is not None
            high, low, nanos = map(int, timestamp.groups())
            origin = (high << 32) + low + nanos / 1e9
            typed = next(
                event
                for event in reversed(events)
                if event["event"] == "start" and event["action"] == "type"
            )
            second = typed["time"] - origin + 0.4
            assert (
                max(
                    abs(a - b) for a, b in zip(video_patch(clip, 0), patch, strict=True)
                )
                <= 3
            )
            changed = video_patch(clip, second)
            assert (
                sum(abs(a - b) > 20 for a, b in zip(changed, patch, strict=True)) > 1000
            )
        else:
            assert (
                max(
                    abs(a - b)
                    for a, b in zip(video_patch(clip, 0.8), patch, strict=True)
                )
                <= 3
            )
        with Image.open(tmp_path / "after.png") as screenshot:
            assert (
                screenshot.convert("RGB").crop((340, 644, 940, 708)).tobytes() == patch
            )
        cli(demo.directory, "key", "Ctrl+a")
        cli(demo.directory, "type", "Between clips")
    assert "Entered: café 日本語 😀" in (demo.directory / "app.log").read_text()


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_demo_word_selection_and_text_menu(demo: Demo, tmp_path: Path) -> None:
    log = demo.directory / "app.log"
    wait_until(lambda: "Demo ready" in log.read_text())
    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "alpha beta")
    cli(demo.directory, "click", "80", "100", "--count", "2")
    cli(demo.directory, "type", "café")
    wait_until(lambda: "Text: café beta\n" in log.read_text())
    cli(demo.directory, "click", "80", "100", "--button", "right")
    cli(demo.directory, "screenshot", "--delay", "0.2", str(tmp_path / "menu.png"))
    # GTK's entry menu opens at the pointer; Select All is below Delete.
    cli(demo.directory, "click", "120", "265")
    cli(demo.directory, "key", "BackSpace")
    cli(demo.directory, "key", "Return")
    wait_until(lambda: "Entered: \n" in log.read_text())


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_demo_slider_scroll_and_reset(demo: Demo) -> None:
    log = demo.directory / "app.log"
    wait_until(lambda: "Demo ready" in log.read_text())
    cli(demo.directory, "click", "54", "266")
    wait_until(lambda: "Option: True" in log.read_text())
    cli(demo.directory, "drag", "147", "382", "350", "382", "--duration", "0.4")

    def value() -> int:
        return int(re.findall(r"Value: (\d+)", log.read_text())[-1])

    wait_until(lambda: "Value:" in log.read_text() and value() > 70)
    before = value()
    cli(demo.directory, "key", "Right")
    wait_until(lambda: value() == before + 1)

    def position() -> tuple[int, int]:
        matches = re.findall(r"Scroll: x=(\d+) y=(\d+)", log.read_text())
        if not matches:
            return (0, 0)
        x, y = matches[-1]
        return int(x), int(y)

    for direction in ["down", "right"]:
        cli(demo.directory, "scroll", "700", "250", direction, "--steps", "3")
    wait_until(lambda: all(coordinate > 0 for coordinate in position()))
    for direction in ["up", "left"]:
        cli(demo.directory, "scroll", "700", "250", direction, "--steps", "8")
    wait_until(lambda: position() == (0, 0))
    for direction in ["down", "right"]:
        cli(demo.directory, "scroll", "700", "250", direction)
    wait_until(lambda: all(coordinate > 0 for coordinate in position()))
    cli(demo.directory, "click", "120", "100")
    # Submitting a long result must not push the neighboring Reset offscreen.
    text = "W" * 150
    cli(demo.directory, "type", "--interval", "0", text)
    cli(demo.directory, "key", "Return")
    wait_until(lambda: f"Entered: {text}\n" in log.read_text())
    cli(demo.directory, "click", "700", "513")
    wait_until(lambda: "Reset\n" in log.read_text())
    assert value() == 25
    assert position() == (0, 0)
    assert "Option: False\n" in log.read_text()
    assert "Text: \n" in log.read_text()
    # Reset returns focus to the entry, so the next keyboard-only action works.
    cli(demo.directory, "type", "fresh")
    cli(demo.directory, "key", "Return")
    wait_until(lambda: "Entered: fresh\n" in log.read_text())


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_demo_hover_tip_and_space_toggle(demo: Demo, tmp_path: Path) -> None:
    log = demo.directory / "app.log"
    wait_until(lambda: "Demo ready" in log.read_text())
    cli(demo.directory, "move", "1100", "600")
    cli(demo.directory, "screenshot", str(tmp_path / "before.png"))
    cli(demo.directory, "move", "150", "266")
    cli(demo.directory, "screenshot", "--delay", "1", str(tmp_path / "tip.png"))
    with (
        Image.open(tmp_path / "before.png") as before,
        Image.open(tmp_path / "tip.png") as after,
    ):
        # The tooltip appears below the option, outside the checkbox's hover styling.
        region = (20, 290, 480, 335)
        assert ImageChops.difference(before.crop(region), after.crop(region)).getbbox()
    cli(demo.directory, "click", "54", "266")
    wait_until(lambda: "Option: True\n" in log.read_text())
    cli(demo.directory, "key", "Space")
    wait_until(lambda: "Option: False\n" in log.read_text())


@pytest.mark.integration
@pytest.mark.parametrize("missing_executable", [False, True])
def test_x11_failed_app_startup_cleans_up(
    tmp_path: Path, missing_executable: bool
) -> None:
    directory = tmp_path / "failed-session"
    for _ in range(2):
        app = (
            ["/framewisp-no-such-executable"]
            if missing_executable
            else [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps({k: os.environ[k] for k in ('DISPLAY', 'XDG_RUNTIME_DIR')}), flush=True); raise SystemExit(7)",
            ]
        )
        result = subprocess.run(
            ["framewisp", str(directory), "run", "--x11", "--", *app],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert result.returncode == (1 if missing_executable else 7), result.stderr
        assert not (directory / "session.json").exists()
        if not missing_executable:
            env = json.loads((directory / "app.log").read_text())
            assert not Path(env["XDG_RUNTIME_DIR"]).exists()
            assert not (Path("/tmp/.X11-unix") / f"X{env['DISPLAY'][1:]}").exists()


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["x11"], indirect=True)
def test_x11_unicode_mapping_limit_and_reuse(demo: Demo) -> None:
    log = demo.directory / "app.log"
    cli(demo.directory, "click", "120", "100")
    too_many = "".join(chr(0x4E00 + index) for index in range(129))
    result = subprocess.run(
        ["framewisp", str(demo.directory), "type", "--interval", "0", too_many],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert "128 distinct" in result.stderr
    assert "Text:" not in log.read_text()
    for text in [too_many[:128], too_many[:3], too_many[3:6]]:
        cli(demo.directory, "key", "Ctrl+a")
        cli(demo.directory, "type", "--interval", "0", text)
        cli(demo.directory, "key", "Return")
        wait_until(lambda: f"Entered: {text}\n" in log.read_text())

    before = log.read_text()
    rejected = subprocess.run(
        ["framewisp", str(demo.directory), "type", "é"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert rejected.returncode == 1
    assert "per session" in rejected.stderr
    assert log.read_text() == before


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["x11"], indirect=True)
def test_x11_busy_app_keeps_queued_unicode(demo: Demo) -> None:
    state = json.loads((demo.directory / "session.json").read_text())
    app_pid = state["processes"]["app"]
    cli(demo.directory, "click", "120", "100")
    os.kill(app_pid, signal.SIGSTOP)
    try:
        cli(demo.directory, "type", "é", "--interval", "0")
        cli(demo.directory, "type", "中", "--interval", "0")
    finally:
        os.kill(app_pid, signal.SIGCONT)
    cli(demo.directory, "key", "Return")
    wait_until(lambda: "Entered: é中\n" in (demo.directory / "app.log").read_text())


def input_request(
    demo: Demo, action: str, parameters: dict[str, object]
) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(5)
    connection.connect(str(demo.runtime / "control.sock"))
    connection.sendall(
        (
            json.dumps(
                {
                    "session": str(demo.directory),
                    "action": action,
                    "parameters": parameters,
                }
            )
            + "\n"
        ).encode()
    )
    return connection


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe", "x11-probe"], indirect=True)
def test_concurrent_gestures_and_disconnect_release_input(demo: Demo) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "screenshot", str(demo.directory / "ready.png"))
    with input_request(
        demo,
        "drag",
        {
            "x1": 100,
            "y1": 100,
            "x2": 500,
            "y2": 300,
            "duration": 30,
            "button": "left",
            "modifier": ["ctrl", "shift"],
        },
    ) as drag:
        wait_until(
            lambda: any(event["event"] == "press" for event in input_events(demo))
        )
        with input_request(
            demo,
            "click",
            {"x": 600, "y": 100, "button": "right", "count": 1, "modifier": []},
        ) as click:
            started = time.monotonic()
            assert (
                json.loads(cli(demo.directory, "status").stdout)["status"] == "running"
            )
            assert time.monotonic() - started < 2
            assert (
                len(
                    [event for event in input_events(demo) if event["event"] == "press"]
                )
                == 1
            )
            drag.close()
            response = json.loads(click.recv(4096))
            assert response["error"] is None
        wait_until(
            lambda: len(
                [event for event in input_events(demo) if event["event"] == "release"]
            )
            == 2
        )
    buttons = [
        event for event in input_events(demo) if event["event"] in {"press", "release"}
    ]
    assert [event["event"] for event in buttons] == [
        "press",
        "release",
        "press",
        "release",
    ]
    assert buttons[0]["ctrl"] and buttons[0]["shift"]
    assert buttons[2]["button"] == 3
    assert not buttons[2]["ctrl"] and not buttons[2]["shift"]
    logged = [
        json.loads(line)
        for line in (demo.directory / "inputs.jsonl").read_text().splitlines()
    ]
    assert [(event["action"], event["event"]) for event in logged] == [
        ("drag", "start"),
        ("drag", "end"),
        ("click", "start"),
        ("click", "end"),
    ]
    assert "cancelled" in logged[1]["error"]
    assert logged[3]["returncode"] == 0


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe", "x11-probe"], indirect=True)
@pytest.mark.parametrize(
    "text",
    [
        "abcd",
        "é中🙂a",
        "".join(chr(code) for code in range(32, 127))
        + "".join(chr(0x4E00 + index) for index in range(128)),
    ],
)
def test_cancel_paced_typing_and_stop(demo: Demo, text: str) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "click", "100", "425")
    with subprocess.Popen(
        ["framewisp", str(demo.directory), "type", text, "--interval", "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as typing:
        wait_until(
            lambda: any(event.get("text") == text[0] for event in input_events(demo))
        )
        started = time.monotonic()
        typing.terminate()
        typing.wait(timeout=2)
        cli(demo.directory, "key", "b")
        assert time.monotonic() - started < 2
    wait_until(
        lambda: any(event.get("text") == text[0] + "b" for event in input_events(demo))
    )
    with input_request(
        demo,
        "drag",
        {
            "x1": 100,
            "y1": 100,
            "x2": 500,
            "y2": 300,
            "duration": 30,
            "button": "left",
            "modifier": ["ctrl"],
        },
    ) as drag:
        wait_until(
            lambda: any(
                event["event"] == "press" and event["ctrl"]
                for event in input_events(demo)
            )
        )
        started = time.monotonic()
        cli(demo.directory, "stop")
        assert time.monotonic() - started < 3
        assert "cancelled" in json.loads(drag.recv(4096))["error"]
    assert demo.process.wait(timeout=2) == 0


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
def test_reject_invalid_input_without_poisoning_connection(demo: Demo) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    invalid: list[tuple[str, dict[str, object]]] = [
        ("key", {"chord": "ctrl+unsupported"}),
        (
            "drag",
            {
                "x1": 1,
                "y1": 1,
                "x2": 100,
                "y2": 100,
                "duration": -1,
                "button": "left",
                "modifier": ["ctrl"],
            },
        ),
        ("click", {"x": 1, "y": 1, "count": 1, "button": [], "modifier": []}),
        ("type", {"text": "a\nb", "interval": 0}),
        ("type", {"text": "é", "interval": 1e308}),
        (
            "drag",
            {
                "x1": 1,
                "y1": 1,
                "x2": 100,
                "y2": 100,
                "duration": 1e308,
                "button": "left",
                "modifier": ["ctrl"],
            },
        ),
    ]
    for action, parameters in invalid:
        with input_request(demo, action, parameters) as connection:
            assert json.loads(connection.recv(4096))["error"] is not None
    cli(demo.directory, "key", "a")
    wait_until(lambda: any(event.get("key") == "a" for event in input_events(demo)))
    assert not any(event.get("key") == "Control_L" for event in input_events(demo))


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
def test_lost_vnc_connection_does_not_replay_input(demo: Demo) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "screenshot", str(demo.directory / "ready.png"))
    with input_request(
        demo,
        "drag",
        {
            "x1": 100,
            "y1": 100,
            "x2": 500,
            "y2": 300,
            "duration": 30,
            "button": "left",
            "modifier": ["ctrl"],
        },
    ) as drag:
        wait_until(
            lambda: any(event["event"] == "press" for event in input_events(demo))
        )
        with input_request(demo, "key", {"chord": "z"}) as queued:
            state = json.loads((demo.directory / "session.json").read_text())
            os.kill(state["processes"]["wayvnc"], signal.SIGTERM)
            demo.process.wait(timeout=5)
            assert json.loads(drag.recv(4096))["error"] is not None
            assert json.loads(queued.recv(4096))["error"] is not None
    assert not any(event.get("key") == "z" for event in input_events(demo))
    assert not (demo.directory / "session.json").exists()


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["qt", "x11-qt"], indirect=True)
def test_qt_menu_survives_between_commands(demo: Demo, tmp_path: Path) -> None:
    log = demo.directory / "app.log"
    wait_until(lambda: "Menu probe ready" in log.read_text())
    # The QML component can finish before the compositor maps its window.
    cli(demo.directory, "screenshot", "--delay", "0.3", str(tmp_path / "ready.png"))
    cli(demo.directory, "click", "200", "150", "--button", "right")
    wait_until(lambda: "Menu opened" in log.read_text())
    cli(demo.directory, "screenshot", "--delay", "0.3", str(tmp_path / "menu.png"))
    assert "Menu closed" not in log.read_text()
    cli(demo.directory, "key", "Down")
    cli(demo.directory, "key", "Return")
    wait_until(lambda: "Item chosen" in log.read_text())


def inspect(demo: Demo, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        ["framewisp", str(demo.directory), "inspect", "--json", *arguments],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode in {0, 1}, result.stderr
    observation: dict[str, Any] = json.loads(result.stdout)
    assert result.returncode == (0 if observation["status"] == "ok" else 1)
    return observation


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_inspect_reads_demo_without_screenshots(demo: Demo) -> None:
    wait_until(lambda: "Demo ready" in (demo.directory / "app.log").read_text())
    button = inspect(demo, "--role", "button", "--name", "Apply text")
    assert button["status"] == "ok", button
    assert button["match_count"] == 1
    nodes = button["matches"]
    assert isinstance(nodes, list)
    assert nodes[0]["name"] == "Apply text"
    assert "click" in nodes[0]["actions"]
    assert nodes[0]["bounds"]["coordinate_space"] == "window"
    assert nodes[0]["bounds"]["width"] == 400
    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "--interval", "0", "Inspected text")
    cli(demo.directory, "click", "120", "170")
    entry = inspect(demo, "--role", "text box", "--text", "Inspected text")
    assert entry["status"] == "ok", entry
    assert entry["match_count"] == 1
    label = inspect(demo, "--role", "label", "--text", "Applied: Inspected text")
    assert label["status"] == "ok", label
    assert label["match_count"] == 1
    cli(demo.directory, "click", "54", "266")
    toggle = inspect(demo, "--role", "checkbox")
    toggles = toggle["matches"]
    assert isinstance(toggles, list)
    assert "checked" in toggles[0]["states"]
    slider = inspect(demo, "--role", "slider")
    sliders = slider["matches"]
    assert isinstance(sliders, list)
    assert sliders[0]["value"] == 25
    # A fresh request rereads the app; snapshot IDs are never reusable selectors.
    cli(demo.directory, "click", "700", "513")
    reset = inspect(demo, "--text", "Applied: Inspected text")
    assert reset["status"] == "ok", reset
    assert reset["match_count"] == 0
    assert reset["snapshot_id"] != label["snapshot_id"]


@pytest.mark.integration
def test_inspect_bounds_and_duplicate_matches(demo: Demo) -> None:
    wait_until(lambda: "Demo ready" in (demo.directory / "app.log").read_text())
    # GTK exposes both the button and its child label with the same name.
    duplicates = inspect(demo, "--name", "Apply text")
    assert duplicates["status"] == "ok", duplicates
    assert duplicates["match_count"] == 2
    for args, reason in (
        (("--limit", "1"), "limit"),
        (("--max-depth", "1"), "max-depth"),
        (("--max-nodes", "2"), "max-nodes"),
    ):
        bounded = inspect(demo, *args)
        assert bounded["status"] == "partial", bounded
        assert reason in bounded["reasons"]
    missing = inspect(demo, "--name", "No such widget")
    assert missing["status"] == "ok", missing
    assert missing["matches"] == []
    cli(demo.directory, "type", "--interval", "0", "z" * 1100)
    wait_until(
        lambda: "Text: " + "z" * 1100 + "\n" in (demo.directory / "app.log").read_text()
    )
    truncated = inspect(demo, "--role", "text box")
    assert truncated["status"] == "partial", truncated
    assert "text-limit" in truncated["reasons"]
    nodes: list[dict[str, Any]] = truncated["matches"]
    text = nodes[0]["text"]
    assert isinstance(text, str)
    assert len(text) == 1024
    assert len(json.dumps(truncated)) < 10000


@pytest.mark.integration
def test_inspect_unresponsive_app_does_not_block_runner(demo: Demo) -> None:
    wait_until(lambda: "Demo ready" in (demo.directory / "app.log").read_text())
    assert inspect(demo, "--name", "Apply text")["status"] == "ok"
    state = json.loads((demo.directory / "session.json").read_text())
    pid = state["processes"]["app"]
    os.kill(pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        timed_out = inspect(demo, "--timeout", "0.2")
        assert timed_out["status"] == "timeout", timed_out
        assert time.monotonic() - started < 2
        assert json.loads(cli(demo.directory, "status").stdout)["status"] == "running"
    finally:
        os.kill(pid, signal.SIGCONT)
    assert inspect(demo, "--name", "Apply text")["status"] == "ok"


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["qt", "x11-qt"], indirect=True)
def test_inspect_qt_controls_and_popup_gap(demo: Demo) -> None:
    wait_until(lambda: "Menu probe ready" in (demo.directory / "app.log").read_text())
    cli(demo.directory, "click", "--button", "right", "500", "300")
    wait_until(lambda: "Menu opened" in (demo.directory / "app.log").read_text())
    item = inspect(demo, "--name", "Inspection button")
    assert item["status"] == "ok", item
    assert item["match_count"] == 1
    # The tested Qt Quick Popup.Window is omitted from the accessible tree.
    popup = inspect(demo, "--name", "Choose this item")
    assert popup["status"] == "ok", popup
    assert popup["match_count"] == 0
    cli(demo.directory, "key", "Escape")
    cli(demo.directory, "click", "--button", "right", "500", "300")
    again = inspect(demo, "--name", "Inspection button")
    assert again["status"] == "ok", again
    assert again["snapshot_id"] != item["snapshot_id"]


@pytest.mark.integration
@pytest.mark.parametrize("app", ["framewisp-demo", "sleep"])
def test_inspection_private_buses_and_unsupported_apps(
    demo: Demo, tmp_path: Path, app: str
) -> None:
    first = json.loads((demo.directory / "session.json").read_text())
    second = tmp_path / "second"
    log = tmp_path / "second-runner.log"
    command = (
        [sys.executable, "-c", "import time; time.sleep(60)"]
        if app == "sleep"
        else [app]
    )
    with log.open("w") as output:
        runner = subprocess.Popen(
            [
                "framewisp",
                str(second),
                "run",
                "--",
                *command,
            ],
            env=os.environ
            | {
                "AT_SPI_BUS_ADDRESS": first["accessibility_bus"],
                "DBUS_SESSION_BUS_ADDRESS": first["accessibility_bus"],
            },
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:

        def ready() -> bool:
            assert runner.poll() is None, log.read_text()
            return "Session ready:" in log.read_text()

        wait_until(ready)
        state = json.loads((second / "session.json").read_text())
        assert state["accessibility_bus"] != first["accessibility_bus"]
        if app == "framewisp-demo":
            wait_until(lambda: "Demo ready" in (second / "app.log").read_text())
            cli(second, "type", "--interval", "0", "Second session only")
            result = json.loads(
                cli(second, "inspect", "--json", "--text", "Second session only").stdout
            )
            assert result["match_count"] == 1
            assert inspect(demo, "--text", "Second session only")["match_count"] == 0
        else:
            result = subprocess.run(
                ["framewisp", str(second), "inspect", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 1
            assert json.loads(result.stdout)["status"] == "unsupported"
        cli(second, "stop")
        assert runner.wait(timeout=10) == 0
        assert not Path(state["runtime_directory"]).exists()
        for pid in state["processes"].values():
            assert not Path(f"/proc/{pid}").exists()
        assert (
            inspect(demo, "--role", "button", "--name", "Apply text")["status"] == "ok"
        )
    finally:
        if runner.poll() is None:
            runner.terminate()
        runner.wait(timeout=20)


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_batch_cli_unicode_capture_and_logs(demo: Demo, tmp_path: Path) -> None:
    wait_until(lambda: "Demo ready" in (demo.directory / "app.log").read_text())
    cli(demo.directory, "screenshot", "--delay", "0.2", str(tmp_path / "ready.png"))
    plan = tmp_path / "batch.json"
    capture = tmp_path / "result.png"
    actions = [
        {"action": "click", "x": 120, "y": 100},
        {"action": "type", "text": "HelloGUI é中", "interval": 0},
        {"action": "key", "chord": "Return"},
    ]
    plan.write_text(json.dumps({"actions": actions, "capture": {"path": str(capture)}}))
    result = json.loads(cli(demo.directory, "batch", "--file", str(plan)).stdout)
    assert result["status"] == "completed"
    assert result["verified"] is None
    assert result["completed_actions"] == 3
    assert result["failed_index"] is None
    assert result["failed_phase"] is None
    assert result["artifacts"] == [str(capture)]
    assert result["duration_seconds"] >= sum(
        item["duration_seconds"] for item in result["results"]
    )
    assert result["capture_seconds"] > 0
    with Image.open(capture) as image:
        assert image.size == (1280, 720)
    wait_until(
        lambda: "Entered: HelloGUI é中\n" in (demo.directory / "app.log").read_text()
    )
    logged = [
        json.loads(line)
        for line in (demo.directory / "inputs.jsonl").read_text().splitlines()
    ]
    assert [(event["action"], event["event"]) for event in logged] == [
        (name, phase) for name in ("click", "type", "key") for phase in ("start", "end")
    ]
    assert all(event["returncode"] == 0 for event in logged if event["event"] == "end")
    assert all(a["time"] <= b["time"] for a, b in zip(logged, logged[1:]))


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None], indirect=True)
def test_batch_validates_later_actions_and_capture_before_input(demo: Demo) -> None:
    before = (demo.directory / "inputs.jsonl").read_text()
    invalid: list[dict[str, object]] = [
        {
            "actions": [
                {"action": "key", "chord": "a"},
                {"action": "key", "chord": "bad"},
            ]
        },
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": str(demo.directory / "session.json")},
        },
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": str(demo.directory / "missing" / "image.png")},
        },
    ]
    for parameters in invalid:
        with input_request(demo, "batch", parameters) as connection:
            assert json.loads(connection.recv(4096))["error"] is not None
    assert (demo.directory / "inputs.jsonl").read_text() == before
    cli(demo.directory, "key", "a")


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None], indirect=True)
def test_batch_no_interleaving_through_final_capture(
    demo: Demo, tmp_path: Path
) -> None:
    wait_until(lambda: "Demo ready" in (demo.directory / "app.log").read_text())
    cli(demo.directory, "click", "120", "100")
    with input_request(
        demo,
        "batch",
        {
            "actions": [
                {"action": "type", "text": "ab", "interval": 0.3},
                {"action": "key", "chord": "Return"},
            ],
            "capture": {"path": str(tmp_path / "batch.png"), "delay": 0.3},
        },
    ) as first:
        wait_until(
            lambda: '"action": "type"' in (demo.directory / "inputs.jsonl").read_text()
        )
        with input_request(
            demo, "batch", {"actions": [{"action": "type", "text": "c", "interval": 0}]}
        ) as second:
            assert (
                json.loads(cli(demo.directory, "status").stdout)["status"] == "running"
            )
            assert json.loads(first.recv(8192))["error"] is None
            assert json.loads(second.recv(8192))["error"] is None
    logged = [
        json.loads(line)
        for line in (demo.directory / "inputs.jsonl").read_text().splitlines()
    ]
    assert [event["action"] for event in logged if event["event"] == "start"] == [
        "click",
        "type",
        "key",
        "type",
    ]
    assert logged[-2]["time"] - logged[-3]["time"] >= 0.3
    wait_until(lambda: "Entered: ab\n" in (demo.directory / "app.log").read_text())


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["x11"], indirect=True)
@pytest.mark.parametrize(
    "existing,first,second,failed", [(0, 129, 0, 1), (0, 64, 65, 2), (1, 64, 64, 2)]
)
def test_batch_checks_x11_capacity_before_input(
    demo: Demo, tmp_path: Path, existing: int, first: int, second: int, failed: int
) -> None:
    if existing:
        cli(demo.directory, "type", "é", "--interval", "0")
    before = (demo.directory / "inputs.jsonl").read_text()
    plan = tmp_path / "batch.json"
    plan.write_text(
        json.dumps(
            {
                "actions": [
                    {"action": "key", "chord": "a"},
                    {
                        "action": "type",
                        "text": "".join(chr(0x4E00 + i) for i in range(first)),
                        "interval": 0,
                    },
                    {
                        "action": "type",
                        "text": "".join(chr(0x4F00 + i) for i in range(second)),
                        "interval": 0,
                    },
                    {"action": "key", "chord": "z"},
                ],
                "capture": {"path": str(tmp_path / "never.png")},
            }
        )
    )
    response = subprocess.run(
        ["framewisp", str(demo.directory), "batch", "--file", str(plan)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert response.returncode == 1
    result = json.loads(response.stdout)
    assert result["status"] == "failed"
    assert result["completed_actions"] == 0
    assert result["failed_index"] == failed
    assert result["failed_phase"] == "validation"
    assert result["results"] == []
    assert (demo.directory / "inputs.jsonl").read_text() == before
    assert "per session" in result["error"]
    assert result["artifacts"] == []
    assert not (tmp_path / "never.png").exists()
    cli(demo.directory, "key", "b")


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None], indirect=True)
def test_batch_capture_failure_keeps_completed_results(
    demo: Demo, tmp_path: Path
) -> None:
    parent = tmp_path / "removed"
    parent.mkdir()
    with input_request(
        demo,
        "batch",
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": str(parent / "image.png"), "delay": 0.5},
        },
    ) as connection:
        wait_until(
            lambda: '"event": "end"' in (demo.directory / "inputs.jsonl").read_text()
        )
        parent.rmdir()
        response = json.loads(connection.recv(8192))
    result = response["data"]
    assert response["error"] is not None
    assert result["completed_actions"] == 1
    assert result["failed_index"] is None
    assert result["failed_phase"] == "capture"
    assert result["artifacts"] == []
    cli(demo.directory, "key", "b")


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe", "x11-probe"], indirect=True)
def test_batch_disconnect_releases_gesture_and_skips_tail(
    demo: Demo, tmp_path: Path
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "screenshot", str(tmp_path / "ready.png"))
    with input_request(
        demo,
        "batch",
        {
            "actions": [
                {
                    "action": "drag",
                    "x1": 100,
                    "y1": 100,
                    "x2": 500,
                    "y2": 300,
                    "duration": 30,
                    "modifier": ["ctrl", "shift"],
                },
                {"action": "key", "chord": "z"},
            ],
            "capture": {"path": str(tmp_path / "never.png")},
        },
    ) as batch:
        wait_until(
            lambda: any(event["event"] == "press" for event in input_events(demo))
        )
        batch.close()
        cli(demo.directory, "click", "600", "100", "--button", "right")
    wait_until(
        lambda: len([e for e in input_events(demo) if e["event"] == "release"]) == 2
    )
    presses = [e for e in input_events(demo) if e["event"] == "press"]
    assert not presses[1]["ctrl"] and not presses[1]["shift"]
    assert not any(e.get("key") == "z" for e in input_events(demo))
    assert not (tmp_path / "never.png").exists()
    logged = [
        json.loads(line)
        for line in (demo.directory / "inputs.jsonl").read_text().splitlines()
    ]
    assert [e["action"] for e in logged if e["event"] == "start"] == ["drag", "click"]


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None], indirect=True)
def test_stop_cancels_batch_capture_delay(demo: Demo, tmp_path: Path) -> None:
    with input_request(
        demo,
        "batch",
        {
            "actions": [{"action": "key", "chord": "a"}],
            "capture": {"path": str(tmp_path / "never.png"), "delay": 30},
        },
    ) as connection:
        wait_until(
            lambda: '"event": "end"' in (demo.directory / "inputs.jsonl").read_text()
        )
        start = time.monotonic()
        cli(demo.directory, "stop")
        assert time.monotonic() - start < 3
        result = json.loads(connection.recv(8192))["data"]
    assert result["completed_actions"] == 1
    assert result["failed_phase"] == "capture"
    assert "cancelled" in result["error"]
    assert not (tmp_path / "never.png").exists()


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None], indirect=True)
def test_disconnect_queued_batch_sends_no_input(demo: Demo) -> None:
    with input_request(
        demo, "batch", {"actions": [{"action": "type", "text": "ab", "interval": 30}]}
    ) as first:
        wait_until(
            lambda: '"event": "start"' in (demo.directory / "inputs.jsonl").read_text()
        )
        with input_request(
            demo, "batch", {"actions": [{"action": "key", "chord": "z"}]}
        ) as queued:
            cli(demo.directory, "status")
            queued.close()
        first.close()
        cli(demo.directory, "key", "c")
    logged = [
        json.loads(line)
        for line in (demo.directory / "inputs.jsonl").read_text().splitlines()
    ]
    assert [
        event["parameters"].get("chord")
        for event in logged
        if event["event"] == "start"
    ] == [None, "c"]


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None], indirect=True)
def test_batch_disconnect_during_capture(demo: Demo, tmp_path: Path) -> None:
    cli(demo.directory, "screenshot", str(tmp_path / "ready.png"))
    state = json.loads((demo.directory / "session.json").read_text())
    sway_pid = state["processes"]["sway"]
    os.kill(sway_pid, signal.SIGSTOP)
    try:
        with input_request(
            demo,
            "batch",
            {
                "actions": [{"action": "type", "text": "", "interval": 0}],
                "capture": {"path": str(tmp_path / "cancelled.png")},
            },
        ) as connection:
            wait_until(lambda: bool(list(tmp_path.glob(".framewisp-capture-*"))))
            connection.close()
            started = time.monotonic()
            wait_until(lambda: not list(tmp_path.glob(".framewisp-capture-*")))
            assert time.monotonic() - started < 2
    finally:
        os.kill(sway_pid, signal.SIGCONT)
    assert not (tmp_path / "cancelled.png").exists()
    cli(demo.directory, "key", "a")


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None], indirect=True)
def test_batch_capture_does_not_replace_new_file(demo: Demo, tmp_path: Path) -> None:
    capture = tmp_path / "result.png"
    with input_request(
        demo,
        "batch",
        {
            "actions": [{"action": "type", "text": "", "interval": 0}],
            "capture": {"path": str(capture), "delay": 0.5},
        },
    ) as connection:
        wait_until(
            lambda: '"event": "end"' in (demo.directory / "inputs.jsonl").read_text()
        )
        capture.write_bytes(b"another client's artifact")
        response = json.loads(connection.recv(8192))
    assert response["data"]["failed_phase"] == "capture"
    assert response["data"]["artifacts"] == []
    assert capture.read_bytes() == b"another client's artifact"
    assert not list(tmp_path.glob(".framewisp-capture-*"))


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
def test_lost_vnc_during_batch_reports_partial_result(
    demo: Demo, tmp_path: Path
) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "screenshot", str(tmp_path / "ready.png"))
    with input_request(
        demo,
        "batch",
        {
            "actions": [
                {"action": "key", "chord": "a"},
                {
                    "action": "drag",
                    "x1": 100,
                    "y1": 100,
                    "x2": 500,
                    "y2": 300,
                    "duration": 30,
                    "modifier": ["ctrl"],
                },
                {"action": "key", "chord": "z"},
            ],
            "capture": {"path": str(tmp_path / "never.png")},
        },
    ) as connection:
        wait_until(
            lambda: any(event["event"] == "press" for event in input_events(demo))
        )
        state = json.loads((demo.directory / "session.json").read_text())
        os.kill(state["processes"]["wayvnc"], signal.SIGTERM)
        demo.process.wait(timeout=5)
        result = json.loads(connection.recv(8192))["data"]
    assert result["completed_actions"] == 1
    assert result["failed_index"] == 1
    assert result["status"] == "failed"
    assert not any(event.get("key") == "z" for event in input_events(demo))
    assert not (tmp_path / "never.png").exists()


@pytest.mark.integration
@pytest.mark.parametrize("demo", [None, "x11"], indirect=True)
def test_inspection_reads_state_after_batch(demo: Demo, tmp_path: Path) -> None:
    wait_until(lambda: "Demo ready" in (demo.directory / "app.log").read_text())
    plan = tmp_path / "inspect-batch.json"
    plan.write_text(
        json.dumps(
            {
                "actions": [
                    {"action": "click", "x": 120, "y": 100},
                    {"action": "type", "text": "HelloBatch", "interval": 0},
                    {"action": "click", "x": 120, "y": 170},
                    {
                        "action": "wait",
                        "timeout": 5,
                        "condition": {
                            "role": "label",
                            "name": "Applied:",
                            "field": "text",
                            "equals": "Applied: HelloBatch",
                        },
                    },
                    {
                        "action": "assert",
                        "timeout": 5,
                        "condition": {
                            "role": "button",
                            "name": "Apply text",
                            "field": "enabled",
                            "equals": True,
                        },
                    },
                    {
                        "action": "assert",
                        "timeout": 5,
                        "condition": {"role": "slider", "field": "value", "equals": 25},
                    },
                    {
                        "action": "baseline",
                        "timeout": 5,
                        "condition": {
                            "role": "checkbox",
                            "field": "checked",
                            "equals": True,
                        },
                    },
                    {"action": "click", "x": 54, "y": 266},
                    {
                        "action": "wait",
                        "timeout": 5,
                        "after": 6,
                        "condition": {
                            "role": "checkbox",
                            "field": "checked",
                            "equals": True,
                        },
                    },
                ]
            }
        )
    )
    batch = json.loads(cli(demo.directory, "batch", "--file", str(plan)).stdout)
    assert batch["status"] == "completed"
    assert batch["verified"] is True
    result = inspect(demo, "--role", "label", "--text", "Applied: HelloBatch")
    assert result["status"] == "ok", result
    assert result["match_count"] == 1


def wait_condition(text: str) -> dict[str, object]:
    return {"role": "label", "name": "Result", "field": "text", "equals": text}


def check_step(action: str, text: str, timeout: float = 5) -> dict[str, object]:
    return {"action": action, "condition": wait_condition(text), "timeout": timeout}


def checked_batch(
    demo: Demo, tmp_path: Path, actions: list[dict[str, object]], **options: object
) -> dict[str, Any]:
    path = tmp_path / "checks.json"
    path.write_text(json.dumps({"actions": actions, **options}))
    response = subprocess.run(
        ["framewisp", str(demo.directory), "batch", "--file", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert response.returncode in (0, 1), response.stderr
    data: dict[str, Any] = json.loads(response.stdout)
    assert (response.returncode == 0) == (data["status"] == "completed"), data
    return data


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["waits", "x11-waits"], indirect=True)
@pytest.mark.parametrize("text", ["Done", "replace"])
def test_checks_submit_delayed_and_replaced_widgets(
    demo: Demo, tmp_path: Path, text: str
) -> None:
    wait_until(lambda: "Wait probe ready" in (demo.directory / "app.log").read_text())
    data = checked_batch(
        demo,
        tmp_path,
        [
            check_step("wait", "Waiting"),
            check_step("baseline", text),
            {"action": "type", "text": text, "interval": 0},
            {"action": "key", "chord": "Return"},
            check_step("wait", text) | {"after": 1},
            check_step("assert", text),
        ],
    )
    assert data["verified"] is True, data
    assert data["completed_actions"] == 6
    assert data["results"][0]["observations"] == 1
    assert data["results"][4]["observations"] > 1
    baseline = data["results"][1]["observation"]["snapshot_id"]
    assert data["results"][4]["baseline_snapshot_id"] == baseline
    assert data["results"][4]["observation"]["snapshot_id"] != baseline
    assert data["results"][5]["observation"]["matches"][0]["text"] == text


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["waits"], indirect=True)
@pytest.mark.parametrize(
    "action,text,timeout",
    [("wait", "Never", 0.3), ("assert", "Never", 2), ("baseline", "Waiting", 2)],
)
def test_failed_checks_stop_and_capture(
    demo: Demo, tmp_path: Path, action: str, text: str, timeout: float
) -> None:
    wait_until(lambda: "Wait probe ready" in (demo.directory / "app.log").read_text())
    capture = tmp_path / "failed.png"
    data = checked_batch(
        demo,
        tmp_path,
        [
            check_step("wait", "Waiting"),
            check_step(action, text, timeout),
            {"action": "type", "text": "should-not-run"},
        ],
        failure_capture={"path": str(capture)},
    )
    assert data["verified"] is False
    assert data["completed_actions"] == 1
    assert data["failed_index"] == 1
    assert data["failed_phase"] == "check"
    assert data["results"][1]["condition"] == wait_condition(text)
    observation = data["results"][1]["observation"]
    if observation["status"] == "ok":
        assert observation["matches"][0]["text"] == "Waiting"
    else:
        assert observation["status"] == "timeout"
    assert data["results"][1]["duration_seconds"] < 3
    assert data["artifacts"] == [str(capture)]
    with Image.open(capture) as image:
        assert image.size == (1280, 720)
    assert (demo.directory / "inputs.jsonl").read_text() == ""
    cli(demo.directory, "key", "a")


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["waits"], indirect=True)
def test_wait_duplicate_matches_fail(demo: Demo, tmp_path: Path) -> None:
    wait_until(lambda: "Wait probe ready" in (demo.directory / "app.log").read_text())
    cli(demo.directory, "type", "--interval", "0", "duplicate")
    cli(demo.directory, "key", "Return")
    wait_until(
        lambda: inspect(demo, "--role", "label", "--name", "Result")["match_count"] == 2
    )
    data = checked_batch(demo, tmp_path, [check_step("wait", "duplicate")])
    assert data["verified"] is False
    assert "ambiguous" in data["error"]
    assert data["results"][-1]["observation"]["match_count"] == 2


@pytest.mark.integration
@pytest.mark.parametrize("demo", ["waits"], indirect=True)
@pytest.mark.parametrize("ending", ["disconnect", "stop", "exit"])
def test_wait_cancellation_and_app_exit(
    demo: Demo, tmp_path: Path, ending: str
) -> None:
    wait_until(lambda: "Wait probe ready" in (demo.directory / "app.log").read_text())
    state = json.loads((demo.directory / "session.json").read_text())
    app = state["processes"]["app"]
    os.kill(app, signal.SIGSTOP)
    try:
        with input_request(
            demo,
            "batch",
            {
                "actions": [
                    check_step("wait", "Never", 10),
                    {"action": "key", "chord": "z"},
                ],
                "failure_capture": {"path": str(tmp_path / "never.png")},
            },
        ) as connection:
            time.sleep(0.15)
            assert (
                json.loads(cli(demo.directory, "status").stdout)["status"] == "running"
            )
            started = time.monotonic()
            if ending == "disconnect":
                connection.close()
                # The following input must leave the queue even while AT-SPI is blocked.
                cli(demo.directory, "key", "a")
            elif ending == "stop":
                os.kill(app, signal.SIGCONT)
                cli(demo.directory, "stop")
            else:
                os.kill(app, signal.SIGKILL)
                demo.process.wait(timeout=3)
            assert time.monotonic() - started < 3
            if ending != "disconnect":
                with connection.makefile("r") as response:
                    data = json.loads(response.readline())["data"]
                assert data["verified"] is False
                assert data["failed_index"] == 0
                assert "cancelled" in data["error"]
    finally:
        if Path(f"/proc/{app}").exists():
            os.kill(app, signal.SIGCONT)
    assert not (tmp_path / "never.png").exists()
    assert '"chord": "z"' not in (demo.directory / "inputs.jsonl").read_text()

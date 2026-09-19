import json
import os
import re
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
    recording = (
        tmp_path / "session.mp4"
        if mode is True or mode in {"large", "uncaptioned"}
        else None
    )
    command = ["framewisp", str(directory), "run"]
    if recording is not None:
        command.extend(["--record", str(recording)])
        if mode == "uncaptioned":
            command.append("--no-captions")
    if mode in {"large", "odd"}:
        width, height = (1600, 900) if mode == "large" else (1601, 901)
        command.extend(["--width", str(width), "--height", str(height)])
    probes = {
        "probe": "input_probe.py",
        "scroll": "scroll_probe.py",
        "large": "input_probe.py",
        "odd": "input_probe.py",
    }
    app = (
        [sys.executable, str(Path(__file__).with_name(probes[mode]))]
        if mode in probes
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
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
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
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
def test_clipboard_copy_paste_survives_input_connections(demo: Demo) -> None:
    wait_until(lambda: any(event["event"] == "ready" for event in input_events(demo)))
    cli(demo.directory, "click", "100", "425")
    for text in ["one", "two", "three"]:
        cli(demo.directory, "key", "Ctrl+a")
        cli(demo.directory, "type", "--interval", "0", text)
        for chord in ["Ctrl+a", "Ctrl+c", "Right", "Ctrl+v"]:
            cli(demo.directory, "key", chord)
        wait_until(
            lambda: any(event.get("text") == text * 2 for event in input_events(demo))
        )


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
@pytest.mark.parametrize("demo", ["probe"], indirect=True)
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
@pytest.mark.parametrize("demo", [None, True], indirect=True)
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
        cli(demo.directory, "record-stop")
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

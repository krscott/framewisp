"""Real capture/encoding tests; portal negotiation is isolated from the desktop."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from PIL import Image, ImageChops

from framewisp.attach import parse_attached_request
from framewisp.portal import REMOTE, SCREENCAST, DesktopPortal
from framewisp.teaching import Teaching

# pyright: reportMissingModuleSource=false


@pytest.fixture
def teaching(tmp_path: Path) -> Iterator[Teaching]:
    lesson = Teaching(tmp_path / "lesson", "Apply the customer's settings")
    yield lesson
    if lesson.pipeline is not None:
        from gi.repository import Gst

        lesson.pipeline.set_state(Gst.State.NULL)
    if lesson.video is not None:
        lesson.video.close()


# R-TEACH-1
def test_teaching_portal_requests_only_screen_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portal = object.__new__(DesktopPortal)
    portal.stop = Event()
    portal.bus = Mock()
    calls: list[tuple[str, str]] = []

    def request(
        interface: str,
        method: str,
        signature: str,
        arguments: object,
        options: object,
    ) -> dict[str, object]:
        calls.append((interface, method))
        if method == "CreateSession":
            return {"session_handle": "/test/session"}
        if method == "Start":
            return {"streams": [(7, {"size": (320, 240)})]}
        return {}

    monkeypatch.setattr(portal, "request", request)
    input_call = Mock()
    monkeypatch.setattr(portal, "input", input_call)
    portal.open(observe_only=True)
    assert calls == [
        (SCREENCAST, "CreateSession"),
        (SCREENCAST, "SelectSources"),
        (SCREENCAST, "Start"),
    ]
    assert all(interface != REMOTE for interface, _ in calls)
    input_call.assert_not_called()


# R-TEACH-1
@pytest.mark.parametrize(
    "action",
    ["click", "key", "type", "move", "drag", "scroll", "batch", "record-start"],
)
def test_teaching_rejects_every_input_action(action: str) -> None:
    line = json.dumps(
        {"attachment": "token", "action": action, "parameters": {}}
    ).encode()
    with pytest.raises(ValueError, match="observation-only"):
        parse_attached_request(line, "token", teaching=True)


@pytest.mark.parametrize("text", ["", "  ", 1, "n" * 8001])
def test_teaching_rejects_invalid_notes(text: object) -> None:
    line = json.dumps(
        {"attachment": "token", "action": "teach-note", "parameters": {"text": text}}
    ).encode()
    with pytest.raises(ValueError, match="nonblank"):
        parse_attached_request(line, "token", teaching=True)


# R-TEACH-2
@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_existing_output_is_untouched(tmp_path: Path, kind: str) -> None:
    output = tmp_path / "lesson"
    if kind == "directory":
        output.mkdir()
        (output / "keep").write_text("precious")
    elif kind == "file":
        output.write_text("precious")
    else:
        output.symlink_to(tmp_path / "absent")
    lesson = Teaching(output, "test")
    with pytest.raises(FileExistsError):
        lesson.start("videotestsrc")
    lesson.close()
    if kind == "directory":
        assert list(output.iterdir()) == [output / "keep"]
        assert (output / "keep").read_text() == "precious"
    elif kind == "file":
        assert output.read_text() == "precious"
    else:
        assert output.is_symlink() and not (tmp_path / "absent").exists()


def observe(lesson: Teaching, count: int) -> None:
    deadline = time.monotonic() + 8
    while lesson.count < count:
        lesson.poll()
        assert time.monotonic() < deadline, "No teaching observations arrived"
        time.sleep(0.02)


# R-TEACH-3
def test_bundle_has_playable_video_images_goal_and_timed_notes(
    teaching: Teaching,
) -> None:
    teaching.start(
        "videotestsrc is-live=true pattern=ball ! video/x-raw,width=320,height=240,framerate=30/1"
    )
    observe(teaching, 1)
    teaching.note("Choose this customer; the name is a variable. ✓")
    observe(teaching, 3)
    teaching.close()
    manifest = json.loads((teaching.directory / "lesson.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["goal"] == teaching.goal
    entries = [
        json.loads(line)
        for line in (teaching.directory / "timeline.jsonl").read_text().splitlines()
    ]
    frames = [entry for entry in entries if entry["type"] == "frame"]
    notes = [entry for entry in entries if entry["type"] == "note"]
    assert frames[0]["time"] < notes[0]["time"] < frames[-1]["time"]
    assert notes[0]["text"].endswith("✓")
    with Image.open(teaching.directory / frames[0]["path"]) as image:
        assert image.size == (320, 240)
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(teaching.directory / manifest["video"]),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "vp8"
    assert (stream["width"], stream["height"]) == (320, 240)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(teaching.directory / manifest["video"]),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )


# R-TEACH-4
def test_capture_failure_preserves_partial_evidence(teaching: Teaching) -> None:
    teaching.start("videotestsrc is-live=true ! video/x-raw,width=160,height=120")
    observe(teaching, 1)
    teaching.note("Keep this evidence")
    with pytest.raises(RuntimeError, match="Partial demonstration"):
        teaching.close("The stream disconnected")
    manifest = json.loads((teaching.directory / "lesson.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"] == "The stream disconnected"
    assert (teaching.directory / "frames/000000.png").exists()
    assert "Keep this evidence" in (teaching.directory / "timeline.jsonl").read_text()


def test_sparse_screen_keeps_final_notes_on_the_video_timeline(
    teaching: Teaching,
) -> None:
    from gi.repository import Gst

    teaching.start(
        "appsrc name=source is-live=true format=time caps=video/x-raw,format=RGB,width=160,height=120,framerate=0/1"
    )
    assert teaching.pipeline is not None
    source = teaching.pipeline.get_by_name("source")
    assert source is not None
    # Portal startup can delay the first image; keep capture timestamps intact.
    time.sleep(0.4)
    buffer = Gst.Buffer.new_allocate(None, 160 * 120 * 3, None)
    assert buffer is not None
    buffer.fill(0, bytes([255, 0, 0]) * (160 * 120))
    buffer.pts = 0
    assert source.emit("push-buffer", buffer) == Gst.FlowReturn.OK
    observe(teaching, 2)
    teaching.note("This unchanged screen is the final result")
    time.sleep(0.2)
    teaching.close()
    entries = [
        json.loads(line)
        for line in (teaching.directory / "timeline.jsonl").read_text().splitlines()
    ]
    note_time = next(entry["time"] for entry in entries if entry["type"] == "note")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_frames",
            "-of",
            "json",
            str(teaching.directory / "demonstration.webm"),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    frames = json.loads(probe.stdout)["frames"]
    end = float(frames[-1]["best_effort_timestamp_time"]) + float(
        frames[-1]["duration_time"]
    )
    assert end >= note_time - 0.07
    assert len(frames) > 1
    detail = teaching.directory / "detail.png"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-copyts",
            "-i",
            str(teaching.directory / "demonstration.webm"),
            "-ss",
            str(note_time),
            "-frames:v",
            "1",
            "-n",
            str(detail),
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    with Image.open(detail) as image:
        pixel = image.convert("RGB").getpixel((80, 60))
        assert isinstance(pixel, tuple)
        red, green, blue = pixel
        assert red > 200 and green < 20 and blue < 20


def test_source_ending_is_a_failure_even_when_a_last_image_remains(
    teaching: Teaching,
) -> None:
    teaching.start(
        "videotestsrc is-live=true num-buffers=30 ! video/x-raw,width=160,height=120,framerate=30/1"
    )
    with pytest.raises(RuntimeError, match="ended unexpectedly"):
        observe(teaching, 4)
    with pytest.raises(RuntimeError, match="Partial demonstration"):
        teaching.close()
    assert (
        json.loads((teaching.directory / "lesson.json").read_text())["status"]
        == "failed"
    )


def test_teach_requires_user_terminal_and_reports_the_right_command(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(tmp_path / "session"),
            "teach",
            str(tmp_path / "lesson"),
            "--goal",
            "Show a task",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "teach" in result.stderr and "--goal" in result.stderr
    assert not (tmp_path / "lesson").exists()


@pytest.fixture
def desktop_demo(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Run the bundled app on an isolated desktop, never the user's display."""
    read, write = os.pipe()
    server = subprocess.Popen(
        [
            "Xvfb",
            "-displayfd",
            str(write),
            "-screen",
            "0",
            "1280x720x24",
            "-nolisten",
            "tcp",
        ],
        pass_fds=[write],
        stderr=subprocess.DEVNULL,
    )
    os.close(write)
    app: subprocess.Popen[bytes] | None = None
    try:
        assert select.select([read], [], [], 5)[0], "Xvfb did not start"
        display = ":" + os.read(read, 64).decode().strip()
        environment = dict(
            os.environ,
            DISPLAY=display,
            GDK_BACKEND="x11",
            GTK_A11Y="none",
            GSETTINGS_BACKEND="memory",
        )
        environment.pop("XAUTHORITY", None)
        log = tmp_path / "demo.log"
        with log.open("wb") as output:
            app = subprocess.Popen(
                ["dbus-run-session", "--", sys.executable, "-m", "framewisp.demo"],
                env=environment,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while "Demo ready" not in log.read_text():
                assert app.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)
            yield display, log
    finally:
        os.close(read)
        if app is not None:
            os.killpg(app.pid, signal.SIGTERM)
            app.wait(timeout=5)
        server.terminate()
        server.wait(timeout=5)


def test_teaching_captures_a_demonstrated_task_in_the_real_demo(
    desktop_demo: tuple[str, Path], teaching: Teaching
) -> None:
    display, log = desktop_demo
    teaching.start(
        f"ximagesrc display-name={display} show-pointer=true use-damage=false ! video/x-raw,framerate=30/1"
    )
    observe(teaching, 1)
    teaching.note("Type a variable value and apply it")
    result = subprocess.run(
        [
            "xdotool",
            "search",
            "--name",
            "Framewisp demo",
            "windowfocus",
            "mousemove",
            "120",
            "100",
            "click",
            "1",
            "type",
            "LearnMe",
        ],
        env=dict(os.environ, DISPLAY=display, XAUTHORITY=""),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    subprocess.run(
        ["xdotool", "mousemove", "120", "170", "click", "1"],
        env=dict(os.environ, DISPLAY=display, XAUTHORITY=""),
        check=True,
        timeout=5,
    )
    observe(teaching, teaching.count + 3)
    teaching.close()
    assert "Applied: LearnMe" in log.read_text()
    images = sorted((teaching.directory / "frames").iterdir())
    with Image.open(images[0]) as first, Image.open(images[-1]) as last:
        assert first.size == last.size == (1280, 720)
        assert (
            ImageChops.difference(first.convert("RGB"), last.convert("RGB")).getbbox()
            is not None
        )

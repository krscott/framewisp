import json
import socket
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from framewisp.actions import InputAction
from framewisp.batch import DEFAULTS, Batch
from framewisp.connection import HeadlessSessionMetadata
from framewisp.errors import SessionError
from framewisp.inputs import InputWorker


def headless_session(session_dir: Path) -> HeadlessSessionMetadata:
    return HeadlessSessionMetadata(
        session_directory=session_dir,
        command_socket=session_dir / "command.sock",
        runtime_directory=session_dir,
        accessibility_bus=f"unix:path={session_dir / 'accessibility.sock'}",
        wayland_display="wayland-1",
        width=800,
        height=600,
    )


class HoldingClient:
    """Records input and interrupts the command once it holds a button."""

    def __init__(self, interrupt: Event) -> None:
        self.interrupt = interrupt
        self.released = Event()
        self.calls: list[tuple[str, object]] = []

    def keyDown(self, key: str) -> None:
        self.calls.append(("keyDown", key))

    def keyUp(self, key: str) -> None:
        self.calls.append(("keyUp", key))
        self.released.set()

    def mouseDown(self, button: int) -> None:
        self.calls.append(("mouseDown", button))
        self.interrupt.set()

    def mouseUp(self, button: int) -> None:
        self.calls.append(("mouseUp", button))

    def mouseMove(self, x: int, y: int) -> None:
        self.calls.append(("mouseMove", (x, y)))

    def disconnect(self) -> None:
        pass


# R-INPUT-5
@pytest.mark.parametrize("cause", ["stop", "disconnect"])
def test_interrupted_drag_releases_held_input(tmp_path: Path, cause: str) -> None:
    stop = Event()
    interrupted = Event()
    client = HoldingClient(stop if cause == "stop" else interrupted)
    worker = InputWorker(
        headless_session(tmp_path),
        client,
        stop,
        screenshot=lambda _path, _cancelled: 0,
    )
    caller, runner = socket.socketpair()
    action = InputAction.parse(
        "drag",
        {
            "x1": 1,
            "y1": 1,
            "x2": 100,
            "y2": 100,
            "duration": 30,
            "button": "left",
            "modifier": ["ctrl"],
            "move_duration": 0,
        },
    )
    try:
        assert worker.submit(runner, action)
        if cause == "disconnect":
            assert interrupted.wait(timeout=5)
            caller.close()
            # Stopping the worker now would hide a missed disconnect.
            assert client.released.wait(timeout=5)
        else:
            reply = json.loads(caller.recv(4096))
            assert "cancelled" in reply["error"]
    finally:
        caller.close()
        worker.close()
    assert client.calls[-2:] == [("mouseUp", 1), ("keyUp", "ctrl")]


class FailingPress(HoldingClient):
    def mouseDown(self, button: int) -> None:
        raise SessionError("button refused")


def logged_records(session_dir: Path) -> list[dict[str, object]]:
    lines = (session_dir / "inputs.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def button_events(session_dir: Path) -> list[tuple[object, object, object]]:
    return [
        (record["event"], record["x"], record["y"])
        for record in logged_records(session_dir)
        if record["event"] in {"press", "release"}
    ]


def perform(
    session_dir: Path,
    work: InputAction | Batch,
    client: HoldingClient | None = None,
) -> dict[str, object]:
    stop = Event()
    worker = InputWorker(
        headless_session(session_dir),
        client if client is not None else HoldingClient(Event()),
        stop,
        screenshot=lambda _path, _cancelled: 0,
    )
    caller, runner = socket.socketpair()
    try:
        assert worker.submit(runner, work)
        return cast(dict[str, object], json.loads(caller.recv(65536)))
    finally:
        caller.close()
        worker.close()


def action(name: str, **parameters: object) -> InputAction:
    return InputAction.parse(
        name, DEFAULTS[name] | {"move_duration": 0} | dict(parameters)
    )


# R-REC-5
@pytest.mark.parametrize(
    ("gesture", "expected"),
    [
        (action("click", x=10, y=20, button="left", count=1), [("press", 10, 20)]),
        (action("click", x=10, y=20, button="right", count=1), [("press", 10, 20)]),
        (
            action("click", x=10, y=20, button="left", count=2),
            [("press", 10, 20)] * 2,
        ),
        (
            action("drag", x1=1, y1=2, x2=30, y2=40, duration=0.05),
            [("press", 1, 2), ("release", 30, 40)],
        ),
        (action("move", x=10, y=20), []),
        (action("scroll", x=10, y=20, direction="down", ticks=2), []),
    ],
)
def test_log_marks_presses_and_drag_releases(
    tmp_path: Path, gesture: InputAction, expected: list[tuple[str, int, int]]
) -> None:
    assert perform(tmp_path, gesture)["error"] is None
    assert button_events(tmp_path) == expected


# R-REC-5
def test_batched_click_logs_its_press(tmp_path: Path) -> None:
    batch = Batch((action("click", x=10, y=20, button="left", count=1),), None)
    assert perform(tmp_path, batch)["error"] is None
    assert button_events(tmp_path) == [("press", 10, 20)]


# R-REC-6
def test_press_and_release_use_the_clamped_position(tmp_path: Path) -> None:
    perform(tmp_path, action("click", x=5000, y=6000, button="left", count=1))
    perform(tmp_path, action("drag", x1=1, y1=2, x2=5000, y2=6000, duration=0.05))
    assert button_events(tmp_path) == [
        ("press", 799, 599),
        ("press", 1, 2),
        ("release", 799, 599),
    ]


# R-REC-6
def test_log_times_each_press_not_its_command(tmp_path: Path) -> None:
    gesture = action("click", x=9, y=9, button="left", count=2, move_duration=0.2)
    perform(tmp_path, Batch((action("move", x=1, y=1), gesture), None))
    click = [r for r in logged_records(tmp_path) if r["action"] == "click"]
    start, first, second, end = (cast(float, r["time"]) for r in click)
    assert [r["event"] for r in click] == ["start", "press", "press", "end"]
    assert first - start >= 0.2
    assert second - first >= 0.1
    assert end >= second


# R-REC-5
def test_refused_press_is_not_logged(tmp_path: Path) -> None:
    client = FailingPress(Event())
    gesture = action("click", x=10, y=20, button="left", count=1)
    assert "button refused" in str(perform(tmp_path, gesture, client)["error"])
    assert button_events(tmp_path) == []


# R-REC-5
def test_interrupted_drag_logs_its_press_only(tmp_path: Path) -> None:
    stop = Event()
    client = HoldingClient(stop)
    worker = InputWorker(
        headless_session(tmp_path),
        client,
        stop,
        screenshot=lambda _path, _cancelled: 0,
    )
    caller, runner = socket.socketpair()
    try:
        assert worker.submit(
            runner, action("drag", x1=1, y1=2, x2=30, y2=40, duration=30)
        )
        assert "cancelled" in json.loads(caller.recv(4096))["error"]
    finally:
        caller.close()
        worker.close()
    assert ("mouseUp", 1) in client.calls
    assert button_events(tmp_path) == [("press", 1, 2)]

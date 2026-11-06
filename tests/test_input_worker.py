import json
import socket
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from framewisp.actions import InputAction
from framewisp.batch import DEFAULTS, Batch
from framewisp.errors import SessionError
from framewisp.inputs import InputWorker


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
        tmp_path,
        client,
        stop,
        x11=False,
        size=(800, 600),
        capture=lambda _path, _cancelled: 0,
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


def logged_records(session: Path) -> list[dict[str, object]]:
    lines = (session / "inputs.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def pings(session: Path) -> list[tuple[object, object]]:
    return [
        (record["x"], record["y"])
        for record in logged_records(session)
        if record["event"] == "ping"
    ]


def perform(
    session: Path,
    work: InputAction | Batch,
    client: HoldingClient | None = None,
) -> dict[str, object]:
    stop = Event()
    worker = InputWorker(
        session,
        client if client is not None else HoldingClient(Event()),
        stop,
        x11=False,
        size=(800, 600),
        capture=lambda _path, _cancelled: 0,
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
        (action("click", x=10, y=20, button="left", count=1), [(10, 20)]),
        (action("click", x=10, y=20, button="right", count=1), [(10, 20)]),
        (action("click", x=10, y=20, button="left", count=2), [(10, 20)] * 2),
        (
            action("drag", x1=1, y1=2, x2=30, y2=40, duration=0.05),
            [(1, 2), (30, 40)],
        ),
        (action("move", x=10, y=20), []),
        (action("scroll", x=10, y=20, direction="down", steps=2), []),
    ],
)
def test_pings_mark_pointer_presses(
    tmp_path: Path, gesture: InputAction, expected: list[tuple[int, int]]
) -> None:
    assert perform(tmp_path, gesture)["error"] is None
    assert pings(tmp_path) == expected


# R-REC-5
def test_batched_click_pings(tmp_path: Path) -> None:
    batch = Batch((action("click", x=10, y=20, button="left", count=1),), None)
    assert perform(tmp_path, batch)["error"] is None
    assert pings(tmp_path) == [(10, 20)]


# R-REC-6
def test_ping_uses_the_clamped_position(tmp_path: Path) -> None:
    perform(tmp_path, action("click", x=5000, y=6000, button="left", count=1))
    perform(tmp_path, action("drag", x1=1, y1=2, x2=5000, y2=6000, duration=0.05))
    assert pings(tmp_path) == [(799, 599), (1, 2), (799, 599)]


# R-REC-6
def test_ping_times_each_press_not_its_command(tmp_path: Path) -> None:
    gesture = action("click", x=9, y=9, button="left", count=2, move_duration=0.2)
    perform(tmp_path, Batch((action("move", x=1, y=1), gesture), None))
    click = [r for r in logged_records(tmp_path) if r["action"] == "click"]
    start, first, second, end = (cast(float, r["time"]) for r in click)
    assert [r["event"] for r in click] == ["start", "ping", "ping", "end"]
    assert first - start >= 0.2
    assert second - first >= 0.1
    assert end >= second


# R-REC-5
def test_refused_press_has_no_ping(tmp_path: Path) -> None:
    client = FailingPress(Event())
    gesture = action("click", x=10, y=20, button="left", count=1)
    assert "button refused" in str(perform(tmp_path, gesture, client)["error"])
    assert pings(tmp_path) == []


# R-REC-5
def test_interrupted_drag_pings_its_press_only(tmp_path: Path) -> None:
    stop = Event()
    client = HoldingClient(stop)
    worker = InputWorker(
        tmp_path,
        client,
        stop,
        x11=False,
        size=(800, 600),
        capture=lambda _path, _cancelled: 0,
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
    assert pings(tmp_path) == [(1, 2)]

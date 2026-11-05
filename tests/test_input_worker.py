import json
import socket
from pathlib import Path
from threading import Event

import pytest

from framewisp.actions import InputAction
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

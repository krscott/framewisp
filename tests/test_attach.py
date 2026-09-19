"""Cancellation tests replace portal I/O; live consent is tested on the desktop."""

import json
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from framewisp.attach import KEYCODES, AttachedInput
from framewisp.connection import request_attached
from framewisp.portal import DesktopPortal


@pytest.fixture
def portal_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, tuple[object, ...]]]:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def send(self: DesktopPortal, method: str, signature: str, *values: object) -> None:
        calls.append((method, values))

    monkeypatch.setattr(DesktopPortal, "input", send)
    return calls


def input_without_bus(stop: Event) -> AttachedInput:
    portal = object.__new__(DesktopPortal)
    portal.size = (800, 600)
    portal.node = 1
    return AttachedInput(portal, stop)


def test_stop_releases_drag_and_modifiers(
    portal_calls: list[tuple[str, tuple[object, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = Event()
    inputs = input_without_bus(stop)
    original_move = inputs.move

    def move_and_stop(x: int, y: int) -> None:
        original_move(x, y)
        if inputs.buttons:
            stop.set()

    monkeypatch.setattr(inputs, "move", move_and_stop)
    with pytest.raises(InterruptedError, match="disconnected"):
        inputs.perform(
            "drag",
            {
                "x1": 10,
                "y1": 10,
                "x2": 100,
                "y2": 100,
                "button": "left",
                "modifier": ["ctrl", "shift"],
                "duration": 0,
            },
        )
    assert portal_calls[-3:] == [
        ("NotifyPointerButton", (272, 0)),
        ("NotifyKeyboardKeycode", (KEYCODES["shift"], 0)),
        ("NotifyKeyboardKeycode", (KEYCODES["ctrl"], 0)),
    ]
    assert not inputs.keys and not inputs.buttons
    before = list(portal_calls)
    with pytest.raises(InterruptedError):
        inputs.perform("type", {"text": "never sent", "interval": 0})
    assert portal_calls == before


def test_invalid_drag_does_not_press_button(
    portal_calls: list[tuple[str, tuple[object, ...]]],
) -> None:
    inputs = input_without_bus(Event())
    with pytest.raises(ValueError, match="outside"):
        inputs.perform(
            "drag",
            {
                "x1": 10,
                "y1": 10,
                "x2": 900,
                "y2": 100,
                "button": "left",
                "modifier": ["ctrl"],
                "duration": 1,
            },
        )
    assert all(method != "NotifyPointerButton" for method, _ in portal_calls)
    assert portal_calls[-1] == ("NotifyKeyboardKeycode", (KEYCODES["ctrl"], 0))


@pytest.mark.parametrize(
    "arguments", [["key", "a"], ["screenshot", "/tmp/unused.png"], ["detach"]]
)
def test_disconnected_cli(arguments: list[str], tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), *arguments],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert "disconnected" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "inputs.jsonl").exists()


def test_stale_attach_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "kind": "attached",
                "runtime_directory": str(tmp_path / "gone"),
            }
        )
    )
    assert request_attached(tmp_path, "detach", {}) == 1
    assert "disconnected" in capsys.readouterr().err

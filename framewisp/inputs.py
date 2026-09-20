"""Serialized headless input on the runner's persistent VNC connection."""

import json
import math
import os
import select
import socket
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Protocol, cast

from framewisp.captions import log_input
from framewisp.connection import reply
from framewisp.errors import SessionError, display_command
from framewisp.keys import CLICK_BUTTONS, MODIFIERS, SCROLL_BUTTONS, key_commands
from framewisp.x11 import type_text as type_x11_text


class VNC(Protocol):
    def keyDown(self, key: str) -> object: ...
    def keyUp(self, key: str) -> object: ...
    def mouseDown(self, button: int) -> object: ...
    def mouseUp(self, button: int) -> object: ...
    def mouseMove(self, x: int, y: int) -> object: ...
    def disconnect(self) -> None: ...


@dataclass(frozen=True)
class InputAction:
    action: str
    parameters: dict[str, object]

    @staticmethod
    def parse(action: object, raw: object) -> "InputAction":
        if not isinstance(raw, dict) or not isinstance(action, str):
            raise ValueError("Input requires an action and parameters object.")
        p = cast(dict[str, object], raw)
        fields = {
            "move": ("x", "y"),
            "click": ("x", "y", "button", "count", "modifier"),
            "drag": ("x1", "y1", "x2", "y2", "duration", "button", "modifier"),
            "scroll": ("x", "y", "direction", "steps"),
            "type": ("text", "interval"),
            "key": ("chord",),
        }
        if action not in fields or any(name not in p for name in fields[action]):
            raise ValueError("Unsupported or incomplete input action.")
        p = {name: p[name] for name in fields[action]}
        for name, value in p.items():
            if name in {"x", "y", "x1", "y1", "x2", "y2", "count", "steps"}:
                if type(value) is not int:
                    raise ValueError(f"{name} must be an integer.")
                if (
                    name in {"x", "y", "x1", "y1", "x2", "y2"}
                    and not 0 <= value <= 65535
                ):
                    raise ValueError("Coordinates must be between 0 and 65535.")
            elif name in {"duration", "interval"}:
                if (
                    type(value) not in {int, float}
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError(f"{name} must be finite and nonnegative.")
            elif name == "modifier":
                if not isinstance(value, list):
                    raise ValueError("modifier must be a list.")
                values = cast(list[object], value)
                if any(
                    not isinstance(item, str) or item not in MODIFIERS
                    for item in values
                ) or len(set(cast(list[str], values))) != len(values):
                    raise ValueError("Invalid or duplicate modifier.")
            elif not isinstance(value, str):
                raise ValueError(f"{name} must be a string.")
        if "button" in p and p["button"] not in CLICK_BUTTONS:
            raise ValueError("Unsupported button.")
        if "count" in p and p["count"] not in (1, 2):
            raise ValueError("Click count must be 1 or 2.")
        if "steps" in p and cast(int, p["steps"]) < 1:
            raise ValueError("Scroll steps must be positive.")
        if "direction" in p and p["direction"] not in SCROLL_BUTTONS:
            raise ValueError("Unsupported scroll direction.")
        if action == "type" and not all(c.isprintable() for c in cast(str, p["text"])):
            raise ValueError("type supports printable characters only.")
        if action == "key" and key_commands(cast(str, p["chord"])) is None:
            raise ValueError("Unsupported key combination.")
        return InputAction(action, p)


class InputWorker:
    def __init__(self, session: Path, client: VNC, stop: Event, *, x11: bool):
        self.session = session
        self.client = client
        self.stop = stop
        self.x11 = x11
        self.failure: str | None = None
        self.queue: Queue[tuple[socket.socket, InputAction]] = Queue(maxsize=32)
        self.thread = Thread(target=self.run, name="headless-input")
        self.thread.start()

    def submit(self, connection: socket.socket, action: InputAction) -> bool:
        try:
            self.queue.put_nowait((connection, action))
            return True
        except Full:
            return False

    def close(self) -> None:
        self.stop.set()
        self.thread.join()
        if self.failure is not None:
            raise SessionError(self.failure)

    def run(self) -> None:
        while not self.stop.is_set() or not self.queue.empty():
            try:
                connection, action = self.queue.get(timeout=0.05)
            except Empty:
                continue
            with connection:
                error = None
                try:
                    self.perform(connection, action)
                except (InterruptedError, SessionError) as failure:
                    error = str(failure)
                except Exception as failure:
                    # An uncertain transport failure must never replay input or
                    # let another action use the proxy's possibly stale response.
                    self.stop.set()
                    self.client.disconnect()
                    traceback.print_exc()
                    error = f"Input failed; session stopped without retrying: {failure}"
                    self.failure = error
                reply(connection, error=error)

    def perform(self, connection: socket.socket, action: InputAction) -> None:
        def cancelled() -> bool:
            if self.stop.is_set():
                return True
            readable, _, _ = select.select([connection], [], [], 0)
            # Each connection carries exactly one request. EOF or further data
            # cancels it, including a client killed while waiting for its turn.
            return bool(readable)

        def wait(seconds: float = 0) -> None:
            deadline = time.monotonic() + seconds
            while True:
                if cancelled():
                    raise InterruptedError(
                        "Input cancelled; caller disconnected or session stopped."
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.stop.wait(min(remaining, 0.02))

        keys: list[str] = []
        buttons: list[int] = []

        def key(symbol: str, down: bool) -> None:
            wait()
            if down:
                keys.append(symbol)
                self.client.keyDown(symbol)
            else:
                self.client.keyUp(symbol)
                keys.remove(symbol)

        def button(number: int, down: bool) -> None:
            wait()
            if down:
                buttons.append(number)
                self.client.mouseDown(number)
            else:
                self.client.mouseUp(number)
                buttons.remove(number)

        def move(x: int, y: int) -> None:
            wait()
            self.client.mouseMove(x, y)

        p = action.parameters
        wait()
        with log_input(self.session, action.action, p) as logged:
            try:
                modifiers = cast(list[str], p.get("modifier", []))
                for modifier in modifiers:
                    key(modifier, True)
                if action.action in {"move", "click", "scroll", "drag"}:
                    x = cast(int, p["x1" if action.action == "drag" else "x"])
                    y = cast(int, p["y1" if action.action == "drag" else "y"])
                    if self.x11:
                        # Xwayland can discard the first virtual-pointer motion.
                        move(x - 1 if x else 1, y)
                        wait(0.1)
                    move(x, y)
                if action.action == "click":
                    number = CLICK_BUTTONS[cast(str, p["button"])]
                    for index in range(cast(int, p["count"])):
                        if index:
                            wait(0.1)
                        button(number, True)
                        button(number, False)
                elif action.action == "drag":
                    number = CLICK_BUTTONS[cast(str, p["button"])]
                    duration = cast(float, p["duration"])
                    steps = max(1, math.ceil(duration * 60))
                    button(number, True)
                    for step in range(1, steps + 1):
                        wait(duration / steps)
                        move(
                            round(
                                cast(int, p["x1"])
                                + (cast(int, p["x2"]) - cast(int, p["x1"]))
                                * step
                                / steps
                            ),
                            round(
                                cast(int, p["y1"])
                                + (cast(int, p["y2"]) - cast(int, p["y1"]))
                                * step
                                / steps
                            ),
                        )
                    button(number, False)
                elif action.action == "scroll":
                    number = SCROLL_BUTTONS[cast(str, p["direction"])]
                    for _ in range(cast(int, p["steps"])):
                        button(number, True)
                        button(number, False)
                elif action.action == "type":
                    text = cast(str, p["text"])
                    interval = cast(float, p["interval"])
                    if text.isascii():
                        for index, character in enumerate(text):
                            if index:
                                wait(interval)
                            key(character, True)
                            key(character, False)
                    else:
                        type_unicode(
                            self.session, text, interval=interval, cancelled=cancelled
                        )
                elif action.action == "key":
                    commands = key_commands(cast(str, p["chord"]))
                    assert commands is not None
                    for operation, symbol in zip(commands[::2], commands[1::2]):
                        if operation == "key":
                            key(symbol, True)
                            key(symbol, False)
                        else:
                            key(symbol, operation == "keydown")
                for modifier in reversed(modifiers):
                    key(modifier, False)
            finally:
                # Cancellation still releases input. If release itself fails,
                # run() closes the connection and stops the session.
                for number in reversed(buttons):
                    self.client.mouseUp(number)
                for symbol in reversed(keys):
                    self.client.keyUp(symbol)
            logged.returncode = 0


def type_unicode(
    session: Path,
    text: str,
    *,
    interval: float,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    state = json.loads((session / "session.json").read_text())
    env = os.environ | {
        "XDG_RUNTIME_DIR": state["runtime_directory"],
        "WAYLAND_DISPLAY": state["wayland_display"],
    }
    if display := state.get("x11_display"):
        env = env | {
            "DISPLAY": display,
            "XAUTHORITY": "/dev/null",
        }
        return type_x11_text(
            session, text, interval=interval, env=env, cancelled=cancelled
        )
    # wtype uploads a keymap containing the requested characters; wayvnc's US
    # keymap cannot represent arbitrary Unicode. Use keysyms so text never
    # becomes a wtype option, and sleep only between characters.
    pause_ms = math.ceil(interval * 1000)
    arguments = ["wtype"]
    for index, character in enumerate(text):
        if index and pause_ms:
            arguments.extend(["-s", str(pause_ms)])
        arguments.extend(["-k", f"U{ord(character):04X}"])
    duration = max(0, len(text) - 1) * pause_ms / 1000
    # wtype also spends 2 ms on each key press and release.
    return display_command(
        session,
        arguments,
        env=env,
        timeout=15 + duration + len(text) * 0.004,
        cancelled=cancelled,
    )

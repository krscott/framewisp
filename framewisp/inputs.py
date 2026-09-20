"""Serialized headless input on the runner's persistent VNC connection."""

import json
import math
import os
import select
import socket
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Protocol, cast

from framewisp.actions import InputAction
from framewisp.batch import Batch
from framewisp.captions import log_input
from framewisp.checks import Check, perform_check
from framewisp.connection import reply
from framewisp.errors import SessionError, display_command
from framewisp.keys import CLICK_BUTTONS, SCROLL_BUTTONS, key_commands
from framewisp.x11 import (
    MAX_UNICODE_CHARACTERS,
    UNICODE_CAPACITY_ERROR,
    mapped_characters,
)
from framewisp.x11 import type_text as type_x11_text


class VNC(Protocol):
    def keyDown(self, key: str) -> object: ...
    def keyUp(self, key: str) -> object: ...
    def mouseDown(self, button: int) -> object: ...
    def mouseUp(self, button: int) -> object: ...
    def mouseMove(self, x: int, y: int) -> object: ...
    def disconnect(self) -> None: ...


class InputWorker:
    def __init__(
        self,
        session: Path,
        client: VNC,
        stop: Event,
        *,
        x11: bool,
        capture: Callable[[Path, Callable[[], bool]], int],
    ):
        self.session = session
        self.client = client
        self.stop = stop
        self.x11 = x11
        self.capture = capture
        self.failure: str | None = None
        self.queue: Queue[tuple[socket.socket, InputAction | Batch]] = Queue(maxsize=32)
        self.thread = Thread(target=self.run, name="headless-input")
        self.thread.start()

    def submit(self, connection: socket.socket, action: InputAction | Batch) -> bool:
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
                data: dict[str, object] | None = (
                    {} if isinstance(action, Batch) else None
                )
                try:
                    if isinstance(action, Batch):
                        assert data is not None
                        self.perform_batch(connection, action, data)
                    else:
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
                if data is not None:
                    data["status"] = "completed" if error is None else "failed"
                    data["error"] = error
                    if error is not None:
                        data["verified"] = False
                reply(connection, error=error, data=data)

    def cancelled(self, connection: socket.socket) -> bool:
        if self.stop.is_set():
            return True
        readable, _, _ = select.select([connection], [], [], 0)
        # Each connection carries exactly one request. EOF or further data
        # cancels it, including a client killed while waiting for its turn.
        return bool(readable)

    def wait(self, connection: socket.socket, seconds: float = 0) -> None:
        deadline = time.monotonic() + seconds
        while True:
            if self.cancelled(connection):
                raise InterruptedError(
                    "Input cancelled; caller disconnected or session stopped."
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop.wait(min(remaining, 0.02))

    def perform_batch(
        self, connection: socket.socket, batch: Batch, data: dict[str, object]
    ) -> None:
        started = time.monotonic()
        results: list[dict[str, object]] = []
        artifacts: list[str] = []
        data.update(
            results=results,
            artifacts=artifacts,
            completed_actions=0,
            failed_index=None,
            failed_phase=None,
            capture_seconds=None,
            verified=None,
            failure_capture_error=None,
        )
        try:
            self.wait(connection)
            if self.x11:
                # Check at dequeue time so mappings allocated by earlier jobs count.
                characters = mapped_characters(self.session)
                for index, action in enumerate(batch.actions):
                    if action.action == "type":
                        characters.update(
                            char
                            for char in cast(str, action.parameters["text"])
                            if not char.isascii()
                        )
                        if len(characters) > MAX_UNICODE_CHARACTERS:
                            data.update(failed_index=index, failed_phase="validation")
                            raise SessionError(UNICODE_CAPACITY_ERROR)
            for index, action in enumerate(batch.actions):
                action_started = time.monotonic()
                result: dict[str, object] = {
                    "index": index,
                    "action": action.action,
                    "status": "completed",
                    "error": None,
                }
                try:
                    if isinstance(action, Check):
                        if action.after is not None:
                            result["baseline_snapshot_id"] = results[action.after][
                                "baseline_snapshot_id"
                            ]
                        perform_check(
                            self.session,
                            action,
                            result,
                            cancelled=lambda: self.cancelled(connection),
                            wait=lambda seconds: self.wait(connection, seconds),
                        )
                    else:
                        self.perform(connection, action)
                except Exception as error:
                    result.update(status="failed", error=str(error))
                    data.update(
                        failed_index=index,
                        failed_phase="check" if isinstance(action, Check) else "action",
                    )
                    raise
                finally:
                    result["duration_seconds"] = time.monotonic() - action_started
                    results.append(result)
                data["completed_actions"] = index + 1
            if any(
                isinstance(action, Check) and action.action != "baseline"
                for action in batch.actions
            ):
                data["verified"] = True
            if batch.capture is not None:
                capture_started = time.monotonic()
                try:
                    self.wait(connection, batch.capture.delay)
                    self.capture(batch.capture.path, lambda: self.cancelled(connection))
                    artifacts.append(str(batch.capture.path))
                except Exception:
                    data["failed_phase"] = "capture"
                    raise
                finally:
                    data["capture_seconds"] = time.monotonic() - capture_started
        except (SessionError, InterruptedError):
            data["verified"] = False
            if batch.failure_capture is not None and not self.cancelled(connection):
                try:
                    self.wait(connection, batch.failure_capture.delay)
                    self.capture(
                        batch.failure_capture.path, lambda: self.cancelled(connection)
                    )
                    artifacts.append(str(batch.failure_capture.path))
                except (SessionError, InterruptedError, OSError) as error:
                    data["failure_capture_error"] = str(error)
            raise
        finally:
            data["duration_seconds"] = time.monotonic() - started

    def perform(self, connection: socket.socket, action: InputAction) -> None:
        def wait(seconds: float = 0) -> None:
            self.wait(connection, seconds)

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
                            self.session,
                            text,
                            interval=interval,
                            cancelled=lambda: self.cancelled(connection),
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

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
from framewisp.batch import Batch, step_name
from framewisp.captions import KEY_ACTIONS, CaptionCapture, input_error, log_input
from framewisp.checks import Check, perform_check
from framewisp.compact import log_activity
from framewisp.connection import reply
from framewisp.errors import SessionError, display_command
from framewisp.keys import CLICK_BUTTONS, SCROLL_BUTTONS, key_commands
from framewisp.pointer import approach
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
        session_dir: Path,
        client: VNC,
        stop: Event,
        *,
        x11: bool,
        size: tuple[int, int],
        screenshot: Callable[[Path, Callable[[], bool]], int],
        caption_capture: CaptionCapture | None = None,
    ):
        self.session_dir = session_dir
        self.client = client
        self.stop = stop
        self.x11 = x11
        self.size = size
        self.pointer_primed = False
        self.pointer_position: tuple[int, int] | None = None
        self.screenshot = screenshot
        self.caption_capture = caption_capture
        self.failure: str | None = None
        self.queue: Queue[tuple[socket.socket, InputAction | Batch]] = Queue(maxsize=32)
        self.thread = Thread(target=self.run, name="headless-input")
        self.thread.start()

    def submit(self, connection: socket.socket, request: InputAction | Batch) -> bool:
        try:
            self.queue.put_nowait((connection, request))
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
                connection, request = self.queue.get(timeout=0.05)
            except Empty:
                continue
            with connection:
                error = None
                data: dict[str, object] | None = (
                    {} if isinstance(request, Batch) else None
                )
                try:
                    if isinstance(request, Batch):
                        assert data is not None
                        self.perform_batch(connection, request, data)
                    else:
                        self.perform(connection, request)
                except (InterruptedError, SessionError) as failure:
                    error = input_error(
                        "batch" if isinstance(request, Batch) else request.action,
                        failure,
                    )
                except Exception as failure:
                    # An uncertain transport failure must never replay input or
                    # let another action use the proxy's possibly stale response.
                    self.stop.set()
                    self.client.disconnect()
                    detail = input_error(
                        "batch" if isinstance(request, Batch) else request.action,
                        failure,
                    )
                    traceback.print_exception(
                        RuntimeError(detail).with_traceback(failure.__traceback__)
                    )
                    error = f"Input failed; session stopped without retrying: {detail}"
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
            completed_steps=0,
            failed_index=None,
            failed_phase=None,
            screenshot_seconds=None,
            verified=None,
            failure_screenshot_error=None,
        )
        try:
            self.wait(connection)
            if self.x11:
                # Check at dequeue time so mappings allocated by earlier jobs count.
                characters = mapped_characters(self.session_dir)
                for index, step in enumerate(batch.steps):
                    if isinstance(step, InputAction) and step.action == "type":
                        characters.update(
                            char
                            for char in cast(str, step.parameters["text"])
                            if not char.isascii()
                        )
                        if len(characters) > MAX_UNICODE_CHARACTERS:
                            data.update(failed_index=index, failed_phase="validation")
                            raise SessionError(UNICODE_CAPACITY_ERROR)
            for index, step in enumerate(batch.steps):
                step_started = time.monotonic()
                result: dict[str, object] = {
                    "index": index,
                    **step_name(step),
                    "status": "completed",
                    "error": None,
                }
                try:
                    if isinstance(step, Check):
                        if step.after is not None:
                            result["baseline_observation_id"] = results[step.after][
                                "baseline_observation_id"
                            ]
                        with log_activity(self.session_dir):
                            perform_check(
                                self.session_dir,
                                step,
                                result,
                                cancelled=lambda: self.cancelled(connection),
                                wait=lambda seconds: self.wait(connection, seconds),
                            )
                    else:
                        self.perform(connection, step)
                except Exception as error:
                    detail = input_error(
                        step.action if isinstance(step, InputAction) else step.kind,
                        error,
                    )
                    result.update(status="failed", error=detail)
                    data.update(
                        failed_index=index,
                        failed_phase="check" if isinstance(step, Check) else "action",
                    )
                    if isinstance(step, InputAction) and step.action in KEY_ACTIONS:
                        if isinstance(error, InterruptedError):
                            raise InterruptedError(detail) from None
                        if isinstance(error, SessionError):
                            raise SessionError(
                                detail, returncode=error.returncode
                            ) from None
                        raise RuntimeError(detail) from None
                    raise
                finally:
                    result["duration_seconds"] = time.monotonic() - step_started
                    results.append(result)
                data["completed_steps"] = index + 1
            if any(
                isinstance(step, Check) and step.kind != "baseline"
                for step in batch.steps
            ):
                data["verified"] = True
            if batch.screenshot is not None:
                screenshot_started = time.monotonic()
                try:
                    with log_activity(self.session_dir):
                        self.wait(connection, batch.screenshot.delay)
                        self.screenshot(
                            batch.screenshot.path, lambda: self.cancelled(connection)
                        )
                    artifacts.append(str(batch.screenshot.path))
                except Exception:
                    data["failed_phase"] = "screenshot"
                    raise
                finally:
                    data["screenshot_seconds"] = time.monotonic() - screenshot_started
        except (SessionError, InterruptedError):
            data["verified"] = False
            if batch.failure_screenshot is not None and not self.cancelled(connection):
                try:
                    with log_activity(self.session_dir):
                        self.wait(connection, batch.failure_screenshot.delay)
                        self.screenshot(
                            batch.failure_screenshot.path,
                            lambda: self.cancelled(connection),
                        )
                    artifacts.append(str(batch.failure_screenshot.path))
                except (SessionError, InterruptedError, OSError) as error:
                    data["failure_screenshot_error"] = str(error)
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

        def bounded(x: int, y: int) -> tuple[int, int]:
            return min(x, self.size[0] - 1), min(y, self.size[1] - 1)

        def move(x: int, y: int) -> None:
            wait()
            x, y = bounded(x, y)
            self.client.mouseMove(x, y)
            self.pointer_position = (x, y)

        p = action.parameters
        wait()
        with log_input(
            self.session_dir,
            action.action,
            p,
            caption_capture=self.caption_capture,
        ) as logged:

            def ping() -> None:
                assert self.pointer_position is not None
                logged.ping(*self.pointer_position)

            try:
                modifiers = cast(list[str], p.get("modifier", []))
                for modifier in modifiers:
                    key(modifier, True)
                if action.action in {"move", "click", "scroll", "drag"}:
                    start = self.pointer_position
                    x, y = bounded(
                        cast(int, p["x1" if action.action == "drag" else "x"]),
                        cast(int, p["y1" if action.action == "drag" else "y"]),
                    )
                    if self.x11 and not self.pointer_primed:
                        # Xwayland can discard the first virtual-pointer motion.
                        px, py = start if start is not None else (x, y)
                        move(px - 1 if px else 1, py)
                        wait(0.1)
                        move(px, py)
                        self.pointer_primed = True
                    approach(
                        start,
                        (x, y),
                        cast(float, p["move_duration"]),
                        move=move,
                        wait=wait,
                    )
                if action.action == "click":
                    number = CLICK_BUTTONS[cast(str, p["button"])]
                    for index in range(cast(int, p["count"])):
                        if index:
                            wait(0.1)
                        button(number, True)
                        ping()
                        button(number, False)
                elif action.action == "drag":
                    number = CLICK_BUTTONS[cast(str, p["button"])]
                    x1, y1 = bounded(cast(int, p["x1"]), cast(int, p["y1"]))
                    x2, y2 = bounded(cast(int, p["x2"]), cast(int, p["y2"]))
                    duration = cast(float, p["duration"])
                    steps = max(1, math.ceil(duration * 60))
                    button(number, True)
                    ping()
                    for step in range(1, steps + 1):
                        wait(duration / steps)
                        move(
                            round(x1 + (x2 - x1) * step / steps),
                            round(y1 + (y2 - y1) * step / steps),
                        )
                    button(number, False)
                    ping()
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
                            self.session_dir,
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
    session_dir: Path,
    text: str,
    *,
    interval: float,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    state = json.loads((session_dir / "session.json").read_text())
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
            session_dir, text, interval=interval, env=env, cancelled=cancelled
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
        session_dir,
        arguments,
        env=env,
        timeout=15 + duration + len(text) * 0.004,
        cancelled=cancelled,
        input_content=True,
    )

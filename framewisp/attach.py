"""Share an existing desktop without owning its app or compositor processes."""

# pyright: reportMissingModuleSource=false
# Required command fields are validated by argparse before socket dispatch.
# pyright: reportTypedDictNotRequiredAccess=false

import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event, Lock, Thread
from types import FrameType
from typing import TypedDict

from gi.repository import GLib

from framewisp.portal import DesktopPortal, dispatch_events


class InputParameters(TypedDict, total=False):
    x: int
    y: int
    x1: int
    y1: int
    x2: int
    y2: int
    button: str
    count: int
    modifier: list[str]
    duration: float
    direction: str
    steps: int
    text: str
    interval: float
    chord: str
    path: str


# Linux evdev keycodes for the tested desktop's US layout.
KEYCODES = {
    "escape": 1,
    "backspace": 14,
    "tab": 15,
    "return": 28,
    "ctrl": 29,
    "shift": 42,
    "alt": 56,
    "space": 57,
    "up": 103,
    "left": 105,
    "right": 106,
    "down": 108,
    "delete": 111,
}
for first, row in [
    (2, "1234567890"),
    (16, "qwertyuiop"),
    (30, "asdfghjkl"),
    (44, "zxcvbnm"),
]:
    KEYCODES.update({key: first + offset for offset, key in enumerate(row)})
BUTTONS = {"left": 272, "right": 273}


class AttachedInput:
    def __init__(self, portal: DesktopPortal, stop: Event):
        self.portal = portal
        self.stop = stop
        self.keys: list[int] = []
        self.buttons: set[int] = set()

    def wait(self, seconds: float = 0) -> None:
        if self.stop.wait(seconds):
            raise InterruptedError("Attached session disconnected; input cancelled.")

    def key(self, code: int, pressed: bool) -> None:
        self.wait()
        self.portal.input("NotifyKeyboardKeycode", "iu", code, int(pressed))
        if pressed:
            self.keys.append(code)
        else:
            self.keys.remove(code)

    def button(self, name: str, pressed: bool) -> None:
        self.wait()
        number = BUTTONS[name]
        self.portal.input("NotifyPointerButton", "iu", number, int(pressed))
        (self.buttons.add if pressed else self.buttons.discard)(number)

    def move(self, x: int, y: int) -> None:
        self.wait()
        if not (0 <= x < self.portal.size[0] and 0 <= y < self.portal.size[1]):
            raise ValueError(
                f"Coordinates must be inside the shared {self.portal.size[0]}x{self.portal.size[1]} monitor."
            )
        self.portal.input(
            "NotifyPointerMotionAbsolute", "udd", self.portal.node, float(x), float(y)
        )

    def release(self) -> None:
        for method, held in [
            ("NotifyPointerButton", tuple(self.buttons)),
            ("NotifyKeyboardKeycode", tuple(reversed(self.keys))),
        ]:
            for value in tuple(held):
                try:
                    self.portal.input(method, "iu", value, 0)
                except GLib.Error:
                    # Revoking the portal also removes its input devices.
                    pass
        self.buttons.clear()
        self.keys.clear()

    def perform(self, action: str, p: InputParameters) -> None:
        try:
            for modifier in p.get("modifier", []):
                self.key(KEYCODES[modifier], True)
            if action == "move":
                self.move(p["x"], p["y"])
            elif action == "click":
                self.move(p["x"], p["y"])
                # COSMIC can deliver the button before the absolute move settles.
                self.wait(0.05)
                for index in range(p["count"]):
                    if index:
                        self.wait(0.1)
                    self.button(p["button"], True)
                    self.button(p["button"], False)
            elif action == "drag":
                self.move(p["x1"], p["y1"])
                self.wait(0.05)
                # Validate the endpoint before holding a button.
                if not (
                    0 <= p["x2"] < self.portal.size[0]
                    and 0 <= p["y2"] < self.portal.size[1]
                ):
                    raise ValueError("Drag endpoint is outside the shared monitor.")
                self.button(p["button"], True)
                self.wait(0.05)
                count = max(1, math.ceil(p["duration"] * 60))
                for step in range(1, count + 1):
                    self.wait(p["duration"] / count)
                    self.move(
                        round(p["x1"] + (p["x2"] - p["x1"]) * step / count),
                        round(p["y1"] + (p["y2"] - p["y1"]) * step / count),
                    )
                self.wait(0.05)
                self.button(p["button"], False)
            elif action == "scroll":
                self.move(p["x"], p["y"])
                self.wait(0.05)
                direction = p["direction"]
                axis = 0 if direction in {"up", "down"} else 1
                sign = -1 if direction in {"up", "left"} else 1
                for _ in range(p["steps"]):
                    self.wait()
                    self.portal.input("NotifyPointerAxisDiscrete", "ui", axis, sign)
            elif action == "type":
                for index, character in enumerate(p["text"]):
                    if index:
                        self.wait(p["interval"])
                    code = ord(character)
                    symbol = code if code <= 255 else 0x01000000 | code
                    self.wait()
                    self.portal.input("NotifyKeyboardKeysym", "iu", symbol, 1)
                    self.portal.input("NotifyKeyboardKeysym", "iu", symbol, 0)
            elif action == "key":
                *modifiers, key = p["chord"].lower().split("+")
                for modifier in modifiers:
                    self.key(KEYCODES[modifier], True)
                self.key(KEYCODES[key], True)
                self.key(KEYCODES[key], False)
            else:
                raise ValueError(f"Unsupported attached action: {action}")
        finally:
            self.release()


def capture(portal: DesktopPortal, destination: Path, stop: Event, log: Path) -> None:
    descriptor = portal.capture_fd()
    try:
        with log.open("w") as output:
            process = subprocess.Popen(
                [
                    "gst-launch-1.0",
                    "-q",
                    "pipewiresrc",
                    f"fd={descriptor}",
                    f"path={portal.node}",
                    "num-buffers=1",
                    "!",
                    "videoconvert",
                    "!",
                    "pngenc",
                    "snapshot=true",
                    "!",
                    "filesink",
                    f"location={destination}",
                ],
                pass_fds=(descriptor,),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None:
                if stop.wait(0.02):
                    raise InterruptedError(
                        "Attached session disconnected; capture cancelled."
                    )
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Desktop capture timed out. See {log}")
            if process.returncode:
                raise RuntimeError(f"Desktop capture failed. See {log}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    finally:
        os.close(descriptor)


def attach_session(session: Path) -> int:
    session.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = session / "session.json"
    if state.exists():
        raise RuntimeError(
            f"{state} already exists. Use a different session directory."
        )
    stop = Event()
    ready = Event()
    owns_state = False
    lock = Lock()
    workers: list[Thread] = []
    portal = DesktopPortal(stop)
    inputs = AttachedInput(portal, stop)

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }

    def handle(connection: socket.socket) -> None:
        with connection:
            status, error = 0, None
            try:
                connection.settimeout(2)
                with connection.makefile("r") as source:
                    request = json.loads(source.readline())
                action: str = request["action"]
                parameters: InputParameters = request["parameters"]
                if action == "detach":
                    stop.set()
                else:
                    if not ready.is_set():
                        raise RuntimeError("Attach is waiting for desktop permission.")
                    while not lock.acquire(timeout=0.05):
                        inputs.wait()
                    try:
                        inputs.wait()
                        if action == "screenshot":
                            capture(
                                portal,
                                Path(parameters["path"]),
                                stop,
                                session / "capture.log",
                            )
                        elif action.startswith("record-"):
                            raise ValueError(
                                "Recording attached sessions is not supported yet."
                            )
                        else:
                            inputs.perform(action, parameters)
                    finally:
                        lock.release()
            except (OSError, RuntimeError, ValueError, GLib.Error) as failure:
                status, error = 1, str(failure)
            try:
                connection.sendall(
                    (json.dumps({"status": status, "error": error}) + "\n").encode()
                )
            except OSError:
                pass

    def serve(listener: socket.socket) -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            worker = Thread(target=handle, args=(connection,))
            workers[:] = [thread for thread in workers if thread.is_alive()]
            workers.append(worker)
            worker.start()

    try:
        with tempfile.TemporaryDirectory(prefix="framewisp-attach-") as directory:
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(Path(directory) / "attach.sock"))
                listener.listen()
                listener.settimeout(0.05)
                # Reserve ownership before consent so two requests cannot share a session.
                try:
                    metadata = state.open("x")
                except FileExistsError:
                    print(
                        f"{state} already exists. Use a different session directory.",
                        file=sys.stderr,
                    )
                    return 1
                owns_state = True
                with metadata:
                    json.dump(
                        {
                            "kind": "attached",
                            "runtime_directory": directory,
                            "processes": {"attach": os.getpid()},
                        },
                        metadata,
                    )
                    metadata.write("\n")
                (session / "inputs.jsonl").write_text("", encoding="utf-8")
                server = Thread(target=serve, args=(listener,))
                server.start()
                try:
                    print(
                        f"Stop: framewisp {session} detach (bind this command to your desktop escape shortcut). Ctrl+C also stops access.",
                        flush=True,
                    )
                    portal.open()
                    inputs.wait()
                    ready.set()
                    print(
                        f"Attached: {session} ({portal.size[0]}x{portal.size[1]}). Input shares your desktop pointer and focus.",
                        flush=True,
                    )
                    while not stop.wait(0.02):
                        dispatch_events()
                finally:
                    stop.set()
                    server.join()
    except InterruptedError:
        return 0
    except (RuntimeError, GLib.Error) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        stop.set()
        for worker in workers:
            worker.join()
        inputs.release()
        portal.close()
        if owns_state:
            state.unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        print("Detached. The app and desktop are still running.", flush=True)
    return 0

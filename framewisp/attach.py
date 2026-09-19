"""Share an existing desktop without owning its app or compositor processes."""

# pyright: reportMissingModuleSource=false
# Required command fields are validated by argparse before socket dispatch.
# pyright: reportTypedDictNotRequiredAccess=false

import json
import math
import os
import select
import shlex
import signal
import socket
import sys
import time
import traceback
import uuid
from pathlib import Path
from threading import Event, Lock, Thread
from types import FrameType
from typing import TypedDict

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from framewisp.desktop import reserve_desktop, write_state
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
    # Capture lives in the owner process: even SIGKILL closes every capture FD.
    Gst.init(None)
    descriptor = portal.capture_fd()
    pipeline: Gst.Element | None = None
    try:
        pipeline = Gst.parse_launch(
            "pipewiresrc name=source num-buffers=1 ! videoconvert ! "
            "pngenc snapshot=true ! filesink name=sink"
        )
        assert isinstance(pipeline, Gst.Bin)
        source = pipeline.get_by_name("source")
        sink = pipeline.get_by_name("sink")
        assert source is not None and sink is not None
        source.set_property("fd", descriptor)
        source.set_property("path", str(portal.node))
        sink.set_property("location", str(destination))
        bus = pipeline.get_bus()
        assert bus is not None
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not start desktop capture.")
        deadline = time.monotonic() + 10
        while not stop.is_set():
            message = bus.timed_pop_filtered(
                50 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
            )
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    error, detail = message.parse_error()
                    log.write_text(f"{error}\n{detail}\n")
                    raise RuntimeError(f"Desktop capture failed. See {log}")
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Desktop capture timed out.")
        raise InterruptedError("Attached session disconnected; capture cancelled.")
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        os.close(descriptor)


def foreground_terminal(descriptor: int) -> bool:
    try:
        return os.isatty(descriptor) and os.tcgetpgrp(descriptor) == os.getpgrp()
    except OSError:
        return False


def attach_session(session: Path) -> int:
    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or not foreground_terminal(sys.stdin.fileno())
    ):
        print(
            "Desktop attachment must be started by the user in a foreground interactive terminal.\n"
            "Agents: do not allocate a PTY or bypass this check. Ask the user to run:\n"
            f"  framewisp {shlex.quote(str(session))} attach\n"
            "in another desktop terminal, read the instructions, and approve sharing.",
            file=sys.stderr,
        )
        return 1
    print(
        "This shares a monitor and gives agent commands keyboard and pointer control of your live desktop.\n"
        "Input can reach any focused app. Keep this terminal open; do not background or suspend this command.\n"
        "Before continuing, bind BOTH Ctrl+Alt+Escape and Ctrl+Alt+Shift+Escape to:\n"
        "  framewisp --detach\n"
        "The second binding is needed when the agent holds Shift. Test your bindings.\n"
        "Ctrl+C here also stops access. Stop sharing before detaching from tmux or screen.",
        flush=True,
    )
    try:
        confirmation = input(
            "Type ATTACH to confirm the bindings are configured and you understand this access: "
        )
    except (EOFError, KeyboardInterrupt):
        print("\nNot attached.")
        return 1
    if confirmation != "ATTACH":
        print("Not attached.")
        return 1
    if not foreground_terminal(sys.stdin.fileno()):
        print(
            "Not attached: this command is no longer the terminal foreground job.",
            file=sys.stderr,
        )
        return 1
    return run_attachment(session, sys.stdin.fileno())


def run_attachment(session: Path, terminal: int | None) -> int:
    """Own all desktop access. The CLI must complete interactive consent first."""
    stop, ready = Event(), Event()
    lock = Lock()
    workers: list[Thread] = []
    failures: list[BaseException] = []
    portal: DesktopPortal | None = None
    inputs: AttachedInput | None = None
    attachment = uuid.uuid4().hex
    state = session / "session.json"
    owns_state = False

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous = {
        sig: signal.signal(sig, request_stop)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGTSTP)
    }

    def handle(connection: socket.socket) -> None:
        assert portal is not None and inputs is not None
        with connection:
            status, error = 0, None
            try:
                connection.settimeout(2)
                with connection.makefile("r") as source:
                    line = source.readline(1024 * 1024 + 1)
                if len(line) > 1024 * 1024:
                    raise ValueError("Input request is too large.")
                request = json.loads(line)
                if request.get("attachment") != attachment:
                    raise ValueError(
                        "Attached session is disconnected. Ask the user to attach again."
                    )
                action: str = request["action"]
                parameters: InputParameters = request["parameters"]
                if not ready.is_set():
                    raise ValueError("Attach is waiting for desktop permission.")
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
            except (ValueError, InterruptedError, TimeoutError) as failure:
                status, error = 1, str(failure)
            except BaseException as failure:
                status, error = 1, str(failure)
                failures.append(failure)
                stop.set()
                portal.close()
                traceback.print_exc()
            try:
                connection.sendall(
                    (json.dumps({"status": status, "error": error}) + "\n").encode()
                )
            except OSError:
                pass

    def serve(listener: socket.socket, emergency: socket.socket) -> None:
        try:
            while not stop.is_set():
                readable, _, _ = select.select([listener, emergency], [], [], 0.05)
                for endpoint in readable:
                    connection, _ = endpoint.accept()
                    if endpoint is emergency:
                        connection.close()
                        continue
                    worker = Thread(target=handle, args=(connection,), daemon=True)
                    workers[:] = [thread for thread in workers if thread.is_alive()]
                    workers.append(worker)
                    worker.start()
        except BaseException as failure:
            failures.append(failure)
            stop.set()
            if portal is not None:
                portal.close()
            traceback.print_exc()

    try:
        with reserve_desktop() as directory:
            try:
                session.mkdir(mode=0o700, parents=True, exist_ok=True)
                if (
                    state.exists()
                    and json.loads(state.read_text()).get("kind") != "attached"
                ):
                    raise RuntimeError(
                        f"{state} belongs to another session. Use a different directory."
                    )
                with (
                    socket.socket(socket.AF_UNIX) as listener,
                    socket.socket(socket.AF_UNIX) as emergency,
                ):
                    listener.bind(str(directory / "control.sock"))
                    emergency.bind(str(directory / "detach.sock"))
                    listener.listen(32)
                    emergency.listen(32)
                    write_state(
                        state,
                        {
                            "kind": "attached",
                            "attachment": attachment,
                            "runtime_directory": str(directory),
                            "processes": {"attach": os.getpid()},
                        },
                    )
                    owns_state = True
                    write_state(
                        directory / "desktop.json",
                        {"session": str(session), "attachment": attachment},
                    )
                    (session / "inputs.jsonl").write_text("", encoding="utf-8")
                    portal = DesktopPortal(stop)
                    inputs = AttachedInput(portal, stop)
                    server = Thread(
                        target=serve, args=(listener, emergency), daemon=True
                    )
                    server.start()
                    try:
                        portal.open()
                        inputs.wait()
                        ready.set()
                        print(
                            f"Attached: {session} ({portal.size[0]}x{portal.size[1]}). Stop with framewisp --detach or Ctrl+C.",
                            flush=True,
                        )
                        while not stop.wait(0.02):
                            if terminal is not None and not foreground_terminal(
                                terminal
                            ):
                                raise RuntimeError(
                                    "The attach terminal is no longer in the foreground; disconnecting."
                                )
                            dispatch_events()
                    finally:
                        stop.set()
                        portal.close()
                        server.join(timeout=0.2)
                        deadline = time.monotonic() + 0.2
                        for worker in workers:
                            worker.join(timeout=max(0, deadline - time.monotonic()))
            finally:
                stop.set()
                if portal is not None:
                    portal.close()
                if owns_state:
                    state.unlink(missing_ok=True)
                    owns_state = False
    except InterruptedError:
        pass
    except (OSError, RuntimeError, ValueError, GLib.Error) as failure:
        print(f"Could not attach: {failure}", file=sys.stderr)
        return 1
    finally:
        stop.set()
        if portal is not None:
            portal.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        print("Detached. The app and desktop are still running.", flush=True)
    return int(bool(failures))

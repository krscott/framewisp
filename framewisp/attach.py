"""Share an existing desktop without owning its app or compositor processes."""

# pyright: reportMissingModuleSource=false
# `parse_attached_request` validates required command fields before `perform`.
# pyright: reportTypedDictNotRequiredAccess=false

import json
import math
import os
import select
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import TIMEOUT_MAX, Event, Lock, Thread
from types import FrameType
from typing import TypedDict, cast

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from framewisp.actions import InputAction
from framewisp.captions import input_error
from framewisp.connection import (
    parse_command,
    receive_request,
    resolved_request_path,
)
from framewisp.desktop import reserve_desktop, write_state
from framewisp.keys import SHIFTED_KEYS, parse_chord
from framewisp.ownership import refuse_abandoned_ownership, session_lease
from framewisp.portal import DesktopPortal, dispatch_events
from framewisp.release_guard import (
    finish_release_guard,
    start_release_guard,
    x11_desktop,
)
from framewisp.teaching import Teaching
from framewisp.tray import UNAVAILABLE, SharingIndicator


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


# Linux evdev keycodes for the tested desktop's US layout.
KEYCODES = {
    "esc": 1,
    "bsp": 14,
    "tab": 15,
    "enter": 28,
    "ctrl": 29,
    "shift": 42,
    "alt": 56,
    "space": 57,
    "up": 103,
    "left": 105,
    "right": 106,
    "down": 108,
    "delete": 111,
    "home": 102,
    "end": 107,
    "pgup": 104,
    "pgdn": 109,
    "-": 12,
    "=": 13,
    "[": 26,
    "]": 27,
    ";": 39,
    "'": 40,
    "`": 41,
    "\\": 43,
    ",": 51,
    ".": 52,
    "/": 53,
    "f11": 87,
    "f12": 88,
}
KEYCODES.update({f"f{number}": 58 + number for number in range(1, 11)})
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
        if self.stop.wait(min(seconds, TIMEOUT_MAX)):
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
                    # A closed portal refuses input. Wayland compositors then
                    # remove its devices; on X11 the release guard frees them.
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
                parsed = parse_chord(p["chord"])
                if parsed is None:
                    raise ValueError("Unsupported key combination.")
                modifiers, key = parsed
                # Shifted punctuation names/literals imply Shift on a US layout.
                unshifted = {shifted: base for base, shifted in SHIFTED_KEYS.items()}
                if key in unshifted:
                    key = unshifted[key]
                    if "shift" not in modifiers:
                        modifiers.append("shift")
                for modifier in modifiers:
                    self.key(KEYCODES[modifier], True)
                self.key(KEYCODES[key], True)
                self.key(KEYCODES[key], False)
            else:
                raise ValueError(f"Unsupported attached action: {action}")
        finally:
            self.release()


def open_destination(destination: Path) -> int:
    """Open an empty regular file. Failing here fails only this command."""
    failure = f"Cannot save screenshot to {destination}"
    try:
        # Without O_NONBLOCK, a FIFO would hold the input lock until it has a reader.
        output = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK, 0o666)
    except OSError as error:
        raise ValueError(f"{failure}: {error}") from None
    try:
        if stat.S_ISREG(os.fstat(output).st_mode):
            os.ftruncate(output, 0)
            return output
        reason = "not a regular file."
    except OSError as error:
        reason = str(error)
    with suppress(OSError):
        os.close(output)
    raise ValueError(f"{failure}: {reason}")


def capture(portal: DesktopPortal, destination: Path, stop: Event, log: Path) -> None:
    output = open_destination(destination)
    try:
        # Capture lives in the owner process: even SIGKILL closes every capture FD.
        descriptor = portal.capture_fd()
        try:
            write_png(
                f"pipewiresrc fd={descriptor} path={portal.node}",
                output,
                stop,
                log,
            )
        finally:
            os.close(descriptor)
    except BaseException:
        # A close failure must not hide why the capture failed.
        with suppress(OSError):
            os.close(output)
        raise
    try:
        os.close(output)
    except OSError as error:
        # Network filesystems can report a failed write only here.
        raise ValueError(f"Could not write the screenshot: {error}") from None


# Mutter can hand a new PipeWire connection a frame left from an earlier one
# before its current frames, and sets no timestamps to tell them apart.
FRAME_SETTLE = 0.1
CAPTURE_TIMEOUT = 10
CAPTURE_CANCELLED = "Attached session disconnected; capture cancelled."


def write_png(source: str, output: int, stop: Event, log: Path) -> None:
    """Write the newest frame a GStreamer source delivers to a descriptor."""
    Gst.init(None)
    frame = newest_frame(source, stop, log)
    encoder = Gst.parse_launch(
        "appsrc name=frame ! videoconvert ! pngenc snapshot=true ! fdsink name=sink"
    )
    assert isinstance(encoder, Gst.Bin)
    sink = encoder.get_by_name("sink")
    source_element = encoder.get_by_name("frame")
    assert sink is not None and source_element is not None
    sink.set_property("fd", output)
    with playing(encoder):
        source_element.emit("push-sample", frame)
        source_element.emit("end-of-stream")
        deadline = time.monotonic() + CAPTURE_TIMEOUT
        while not finished(encoder, sink, stop, log, deadline):
            pass


def newest_frame(source: str, stop: Event, log: Path) -> Gst.Sample:
    """Return the newest frame seen within FRAME_SETTLE of the first one."""
    pipeline = Gst.parse_launch(
        # Raw caps keep frames in system memory, as videoconvert needs.
        f"{source} ! appsink name=frames caps=video/x-raw sync=false"
        " max-buffers=1 drop=true"
    )
    assert isinstance(pipeline, Gst.Bin)
    sink = pipeline.get_by_name("frames")
    assert sink is not None
    with playing(pipeline):
        deadline = time.monotonic() + CAPTURE_TIMEOUT
        while (frame := sink.emit("try-pull-sample", 0)) is None:
            if finished(pipeline, None, stop, log, deadline):
                frame = sink.emit("try-pull-sample", 0)
                if frame is None:
                    raise RuntimeError("Desktop capture ended without a frame.")
                break
        if stop.wait(FRAME_SETTLE):
            raise InterruptedError(CAPTURE_CANCELLED)
        frame = sink.emit("try-pull-sample", 0) or frame
        assert isinstance(frame, Gst.Sample)
        # Stopping the source may release the memory behind its buffers.
        buffer = frame.get_buffer()
        assert buffer is not None
        return Gst.Sample.new(buffer.copy_deep(), frame.get_caps(), None, None)


@contextmanager
def playing(pipeline: Gst.Element) -> Generator[None]:
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not start desktop capture.")
        yield
    finally:
        pipeline.set_state(Gst.State.NULL)


def finished(
    pipeline: Gst.Element,
    sink: Gst.Element | None,
    stop: Event,
    log: Path,
    deadline: float,
) -> bool:
    """Wait briefly for end of stream. Raise on failure, timeout, or stop."""
    if stop.is_set():
        raise InterruptedError(CAPTURE_CANCELLED)
    if time.monotonic() >= deadline:
        raise RuntimeError("Desktop capture timed out.")
    bus = pipeline.get_bus()
    assert bus is not None
    message = bus.timed_pop_filtered(
        50 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
    )
    if message is None:
        return False
    if message.type == Gst.MessageType.ERROR:
        error, detail = message.parse_error()
        if sink is not None and message.src == sink:
            # A full disk fails this command, not the attachment, and the log
            # may be on that disk.
            raise ValueError(f"Could not write the screenshot: {error.message}")
        log.write_text(f"{error}\n{detail}\n")
        raise RuntimeError(f"Desktop capture failed. See {log}")
    return True


def parse_attached_request(
    line: bytes, attachment: str, *, teaching: bool = False
) -> InputAction | Path | str:
    """Validate a request: a screenshot destination, or an input action."""
    command = parse_command(line)
    if command.get("attachment") != attachment:
        raise ValueError(
            "Attached session is disconnected. Ask the user to attach again."
        )
    action = command.get("action")
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    if teaching and action not in {"screenshot", "teach-note"}:
        raise ValueError(
            "Teaching is observation-only; input and recording commands are disabled."
        )
    if action.startswith("record-"):
        raise ValueError("Recording attached sessions is not supported yet.")
    raw = command.get("parameters")
    if not isinstance(raw, dict):
        raise ValueError("Input requires an action and parameters object.")
    parameters = cast(dict[str, object], raw)
    if action == "teach-note":
        if not teaching:
            raise ValueError("teach-note requires a teaching session.")
        text = parameters.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError(
                "teach-note requires nonblank text of at most 8000 characters."
            )
        return text
    if action != "screenshot":
        defaults: dict[str, object] = {"modifier": []}
        return InputAction.parse(action, defaults | parameters)
    path = parameters.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("screenshot requires an absolute path.")
    return resolved_request_path(Path(path), "path")


def foreground_terminal(descriptor: int) -> bool:
    try:
        return os.isatty(descriptor) and os.tcgetpgrp(descriptor) == os.getpgrp()
    except OSError:
        return False


def attach_session(
    session: Path,
    *,
    retain_input_content: bool = False,
    teaching: Teaching | None = None,
) -> int:
    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or not foreground_terminal(sys.stdin.fileno())
    ):
        command = (
            f"teach {shlex.quote(str(teaching.directory))} --goal {shlex.quote(teaching.goal)}"
            if teaching is not None
            else "attach"
        )
        print(
            "Desktop attachment must be started by the user in a foreground interactive terminal.\n"
            "Agents: do not allocate a PTY or bypass this check. Ask the user to run:\n"
            f"  framewisp {shlex.quote(str(session))} {command}\n"
            "in another desktop terminal, read the instructions, and approve sharing.",
            file=sys.stderr,
        )
        return 1
    if teaching is not None:
        print(
            "Teaching records your selected monitor and cursor, without keyboard or pointer control.\n"
            "Video, sampled images, and notes are saved unredacted, including any visible secrets.\n"
            f"Goal: {teaching.goal}\nOutput: {teaching.directory}\n"
            "Keep this terminal open and in the foreground. Stop with Ctrl+C here,\n"
            "the sharing indicator, or framewisp --detach. Your apps stay running.",
            flush=True,
        )
    else:
        print(
            "This shares a monitor and gives agent commands keyboard and pointer control of your live desktop.\n"
            "Input can reach any focused app. Keep this terminal open; do not background or suspend this command.\n"
            "Before continuing, bind BOTH Ctrl+Alt+Escape and Ctrl+Alt+Shift+Escape to:\n"
            "  framewisp --detach\n"
            "The second binding is needed when the agent holds Shift. Test your bindings.\n"
            "Ctrl+C here also stops access. Stop sharing before detaching from tmux or screen.",
            flush=True,
        )
    if x11_desktop():
        print(
            "This is an X11 desktop: programs running as you can already read the screen\n"
            "and send input without the desktop dialog. Approving it does not limit them.",
            flush=True,
        )
    try:
        confirmation = input(
            "Type ATTACH to confirm you understand and want to record this demonstration: "
            if teaching is not None
            else "Type ATTACH to confirm the bindings are configured and you understand this access: "
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
    return run_attachment(
        session,
        sys.stdin.fileno(),
        retain_input_content=retain_input_content,
        teaching=teaching,
    )


def run_attachment(
    session: Path,
    terminal: int | None,
    *,
    retain_input_content: bool = False,
    teaching: Teaching | None = None,
) -> int:
    """Reserve session metadata after the CLI completes interactive consent."""
    session = session.resolve()
    try:
        session.mkdir(mode=0o700, parents=True, exist_ok=True)
        with session_lease(session):
            refuse_abandoned_ownership(session)
            return _run_attachment(
                session,
                terminal,
                retain_input_content=retain_input_content,
                teaching=teaching,
            )
    except (OSError, RuntimeError) as failure:
        print(f"Could not attach: {failure}", file=sys.stderr)
        return 1


def _run_attachment(
    session: Path,
    terminal: int | None,
    *,
    retain_input_content: bool,
    teaching: Teaching | None = None,
) -> int:
    """Own all desktop access. The CLI must complete interactive consent first."""
    stop, ready = Event(), Event()
    lock = Lock()
    workers: list[Thread] = []
    failures: list[BaseException] = []
    portal: DesktopPortal | None = None
    inputs: AttachedInput | None = None
    indicator: SharingIndicator | None = None
    guard: subprocess.Popen[bytes] | None = None
    attachment = uuid.uuid4().hex
    state = session / "session.json"
    owns_state = False
    capture_descriptor: int | None = None

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    def announce_ready() -> None:
        assert portal is not None
        ready.set()
        print(
            f"Attached: {session} ({portal.size[0]}x{portal.size[1]}). Stop with framewisp --detach or Ctrl+C.",
            flush=True,
        )

    previous = {
        sig: signal.signal(sig, request_stop)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGTSTP)
    }

    def handle(connection: socket.socket) -> None:
        assert portal is not None and inputs is not None
        with connection:

            def answer(status: int, error: str | None) -> None:
                try:
                    connection.sendall(
                        (json.dumps({"status": status, "error": error}) + "\n").encode()
                    )
                except OSError:
                    pass

            def parse(line: bytes) -> InputAction | Path | str:
                request = parse_attached_request(
                    line, attachment, teaching=teaching is not None
                )
                if not ready.is_set():
                    raise ValueError(
                        "Attach is waiting for desktop permission or the first teaching frame."
                    )
                if (
                    teaching is not None
                    and isinstance(request, Path)
                    and request.is_relative_to(teaching.directory.resolve())
                ):
                    raise ValueError(
                        "Screenshots cannot overwrite the active teaching bundle. Choose a path outside it."
                    )
                return request

            request = receive_request(
                connection, lambda failure: answer(1, failure), parse
            )
            if request is None:
                return
            status, error = 0, None
            action = (
                "screenshot"
                if isinstance(request, Path)
                else "teach-note" if isinstance(request, str) else request.action
            )
            try:
                if isinstance(request, str):
                    inputs.wait()
                    assert teaching is not None
                    teaching.note(request)
                else:
                    while not lock.acquire(timeout=0.05):
                        inputs.wait()
                    try:
                        inputs.wait()
                        if isinstance(request, Path):
                            capture(portal, request, stop, session / "capture.log")
                        else:
                            inputs.perform(
                                action, cast(InputParameters, request.parameters)
                            )
                    finally:
                        lock.release()
            except (ValueError, InterruptedError, TimeoutError) as failure:
                status, error = 1, input_error(
                    action, failure, retain_input_content=retain_input_content
                )
            except BaseException as failure:
                status, error = 1, input_error(
                    action, failure, retain_input_content=retain_input_content
                )
                failures.append(failure)
                stop.set()
                portal.close()
                traceback.print_exception(
                    RuntimeError(error).with_traceback(failure.__traceback__)
                )
            answer(status, error)

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
                            "retain_input_content": retain_input_content,
                            "attachment": attachment,
                            "runtime_directory": str(directory),
                            "processes": {"attach": os.getpid()},
                        },
                    )
                    owns_state = True
                    (session / "app-exit.json").unlink(missing_ok=True)
                    write_state(
                        directory / "desktop.json",
                        {"session": str(session), "attachment": attachment},
                    )
                    (session / "inputs.jsonl").write_text("", encoding="utf-8")
                    if teaching is None:
                        guard = start_release_guard(stop)
                    portal = DesktopPortal(stop)
                    inputs = AttachedInput(portal, stop)
                    server = Thread(
                        target=serve, args=(listener, emergency), daemon=True
                    )
                    server.start()
                    try:
                        if teaching is None:
                            portal.open()
                        else:
                            portal.open(observe_only=True)
                            capture_descriptor = portal.capture_fd()
                            teaching.start(
                                f"pipewiresrc fd={capture_descriptor} path={portal.node}"
                            )
                        inputs.wait()
                        indicator = SharingIndicator(portal.bus, stop)
                        try:
                            indicator.start()
                        except GLib.Error:
                            indicator.close()
                            print(UNAVAILABLE, file=sys.stderr, flush=True)
                        if teaching is None:
                            announce_ready()
                        while not stop.wait(0.02):
                            if terminal is not None and not foreground_terminal(
                                terminal
                            ):
                                raise RuntimeError(
                                    "The attach terminal is no longer in the foreground; disconnecting."
                                )
                            if guard is not None and guard.poll() is not None:
                                raise RuntimeError(
                                    "The X11 input release helper exited; disconnecting."
                                )
                            dispatch_events()
                            if stop.is_set():
                                break
                            if teaching is not None:
                                if not teaching.poll():
                                    dispatch_events()
                                    if portal.revoked:
                                        break
                                    raise RuntimeError(
                                        "Teaching capture ended unexpectedly."
                                    )
                                if not ready.is_set() and teaching.count:
                                    announce_ready()
                    finally:
                        stop.set()
                        if indicator is not None:
                            indicator.close()
                        if teaching is not None:
                            failure = sys.exc_info()[1]
                            try:
                                teaching.close(
                                    (
                                        str(failure)
                                        if failure
                                        else str(failures[0]) if failures else None
                                    ),
                                    revoked=portal.revoked,
                                )
                                if teaching.created:
                                    print(
                                        f"Demonstration saved: {teaching.directory}",
                                        flush=True,
                                    )
                            except (OSError, RuntimeError, GLib.Error) as failure:
                                failures.append(failure)
                                print(str(failure), file=sys.stderr, flush=True)
                        portal.close()
                        server.join(timeout=0.2)
                        deadline = time.monotonic() + 0.2
                        for worker in workers:
                            worker.join(timeout=max(0, deadline - time.monotonic()))
            finally:
                stop.set()
                if portal is not None:
                    portal.close()
                if guard is not None:
                    finish_release_guard(guard)
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
        if capture_descriptor is not None:
            os.close(capture_descriptor)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        print("Detached. The app and desktop are still running.", flush=True)
    return int(bool(failures))

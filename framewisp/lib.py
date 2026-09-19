"""Run one headless app and control it with existing command-line tools."""

import json
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from string import ascii_lowercase, digits
from threading import Event
from types import FrameType

from vncdotool import api

from framewisp.captions import capture_origin, render_captions
from framewisp.x11 import type_text as type_x11_text

SWAY_CONFIG = """\
xwayland disable
primary_selection disabled
output HEADLESS-1 mode {width}x{height}@60Hz
seat seat0 fallback true
input * xkb_layout us
default_border none
"""

KEYS = {
    "return": "enter",
    "tab": "tab",
    "backspace": "bsp",
    "escape": "esc",
    "delete": "delete",
    "left": "left",
    "right": "right",
    "up": "up",
    "down": "down",
    "space": "space",
} | {character: character for character in ascii_lowercase + digits}
MODIFIERS = {"ctrl", "shift", "alt"}
CLICK_BUTTONS = {"left": 1, "right": 3}
SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}


def key_commands(chord: str) -> list[str] | None:
    """Translate a supported chord to VNC commands, or reject it before input."""
    *modifiers, key = chord.lower().split("+")
    if (
        key not in KEYS
        or any(modifier not in MODIFIERS for modifier in modifiers)
        or len(set(modifiers)) != len(modifiers)
    ):
        return None
    arguments: list[str] = []
    for modifier in modifiers:
        arguments.extend(["keydown", modifier])
    symbol = KEYS[key]
    if "shift" in modifiers:
        # wayvnc adjusts modifiers to match the supplied keysym. Send the shifted
        # symbol too, or it clears Shift (and other held modifiers) for this key.
        symbol = symbol.upper() if key in ascii_lowercase else symbol
        if key in digits:
            symbol = ")!@#$%^&*("[int(key)]
        if key == "tab":
            # vncdotool has no ISO_Left_Tab name. It sends a single character's
            # ordinal as the RFB keysym, so encode that keysym directly.
            symbol = chr(0xFE20)
    arguments.extend(["key", symbol])
    for modifier in reversed(modifiers):
        arguments.extend(["keyup", modifier])
    return arguments


@contextmanager
def managed_process(
    command: list[str], *, log: Path, env: dict[str, str]
) -> Generator[subprocess.Popen[bytes], None, None]:
    with log.open("w") as output:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def wait_for_socket(
    runtime: Path,
    pattern: str,
    *,
    process: subprocess.Popen[bytes],
    log: Path,
    stop: Event,
) -> Path | None:
    deadline = time.monotonic() + 10
    while not stop.is_set():
        if process.poll() is not None:
            raise RuntimeError(f"Process exited during startup. See {log}")
        for path in runtime.glob(pattern):
            if path.is_socket():
                return path
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Waiting for {pattern} timed out. See {log}")
        stop.wait(0.05)
    return None


def session_environment_for_run(runtime: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "DISPLAY",
        "XAUTHORITY",
        "WAYLAND_DISPLAY",
        "SWAYSOCK",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        env.pop(key, None)
    env.update(
        XDG_RUNTIME_DIR=str(runtime),
        WLR_BACKENDS="headless",
        WLR_RENDERER="pixman",
        WLR_LIBINPUT_NO_DEVICES="1",
        GDK_BACKEND="wayland",
        GSK_RENDERER="cairo",
    )
    return env


@contextmanager
def start_sway(
    runtime: Path,
    *,
    log: Path,
    env: dict[str, str],
    stop: Event,
    size: tuple[int, int],
    x11: bool = False,
) -> Generator[tuple[subprocess.Popen[bytes], str] | None, None, None]:
    """Yield the process and display name, or None if startup is interrupted."""
    config = runtime / "sway.conf"
    contents = SWAY_CONFIG.format(width=size[0], height=size[1])
    if x11:
        # Sway chooses a free X display. Read its environment from a child,
        # rather than guessing a number or inheriting the host desktop's DISPLAY.
        probe = shlex.join(
            [
                sys.executable,
                "-c",
                "import os; from pathlib import Path; "
                f"Path({str(runtime / 'x11-display')!r}).write_text(os.environ['DISPLAY'])",
            ]
        )
        contents = contents.replace("xwayland disable", "xwayland force")
        contents += f"exec {probe}\n"
    config.write_text(contents)
    with managed_process(["sway", "-c", str(config)], log=log, env=env) as process:
        display = wait_for_socket(
            runtime, "wayland-*", process=process, log=log, stop=stop
        )
        yield (process, display.name) if display is not None else None


def wait_for_x11(
    runtime: Path, *, process: subprocess.Popen[bytes], log: Path, stop: Event
) -> str | None:
    deadline = time.monotonic() + 10
    path = runtime / "x11-display"
    while not stop.is_set():
        if process.poll() is not None:
            raise RuntimeError(f"Compositor exited during X11 startup. See {log}")
        if path.exists() and (display := path.read_text()):
            return display
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Waiting for Xwayland timed out. See {log}")
        stop.wait(0.05)
    return None


@contextmanager
def keep_input_devices(runtime: Path) -> Generator[None, None, None]:
    """Keep the seat's keyboard and pointer present between CLI connections."""
    try:
        with api.connect(str(runtime / "vnc.sock"), timeout=10) as connection:
            connection.pause(0)
            yield
    finally:
        api.shutdown()


@contextmanager
def start_wayvnc(
    runtime: Path, *, log: Path, env: dict[str, str], stop: Event
) -> Generator[subprocess.Popen[bytes] | None, None, None]:
    """Yield the ready process, or None if startup is interrupted."""
    command = ["wayvnc", "-C", "/dev/null", "-k", "us", f"unix:{runtime / 'vnc.sock'}"]
    with managed_process(command, log=log, env=env) as process:
        socket = wait_for_socket(
            runtime, "vnc.sock", process=process, log=log, stop=stop
        )
        yield process if socket is not None else None


@contextmanager
def start_recording(
    destination: Path, *, log: Path, env: dict[str, str], stop: Event, captions: bool
) -> Generator[subprocess.Popen[bytes] | None, None, None]:
    """Wait for the MP4 header before yielding; finalize while Sway is still alive."""
    command = [
        "wf-recorder",
        "-o",
        "HEADLESS-1",
        "-D",
        "-r",
        "30",
        "-c",
        "libx264",
        "-x",
        "yuv420p",
        "-F",
        "scale=out_range=full",
        "-m",
        "mp4",
        "-f",
        str(destination),
    ]
    recorder_env = env | {"WAYLAND_DEBUG": "client"} if captions else env
    with managed_process(command, log=log, env=recorder_env) as process:
        deadline = time.monotonic() + 10
        while not stop.is_set():
            if process.poll() is not None:
                raise RuntimeError(f"Recorder exited during startup. See {log}")
            # wf-recorder opens and writes the MP4 header after receiving a frame.
            if destination.exists() and destination.stat().st_size > 0:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Waiting for recording to start timed out. See {log}"
                )
            stop.wait(0.05)
        else:
            yield None
            return
        origin = capture_origin(log) if captions else 0.0
        try:
            yield process
        finally:
            stopped = time.monotonic()
    if process.returncode != 0:
        raise RuntimeError(f"Recorder failed to finalize {destination}. See {log}")
    if captions:
        render_captions(
            destination,
            input_log=log.parent / "inputs.jsonl",
            origin=origin,
            stopped=stopped,
            log=log.parent / "captions.log",
        )


def recording_path_error(session: Path, destination: Path) -> str | None:
    reserved = {
        (session / name).resolve()
        for name in (
            "session.json",
            ".session.json",
            "sway.log",
            "wayvnc.log",
            "recorder.log",
            "app.log",
            "inputs.jsonl",
            "captions.log",
        )
    }
    if destination in reserved:
        return (
            f"{destination} is reserved for session files. Choose a new recording path."
        )
    if destination.exists():
        return f"{destination} already exists. Choose a new recording path."
    return None


@dataclass
class Recordings:
    session: Path
    env: dict[str, str]
    stop_requested: Event
    size: tuple[int, int]
    process: subprocess.Popen[bytes] | None = None
    resources: ExitStack = field(default_factory=ExitStack)

    def start(self, destination: Path, *, captions: bool = True) -> str | None:
        if self.process is not None:
            return "A recording is already active. Use record-stop first."
        if any(dimension % 2 for dimension in self.size):
            return "Recording requires even display width and height."
        error = recording_path_error(self.session, destination)
        if error is not None:
            return error
        try:
            self.process = self.resources.enter_context(
                start_recording(
                    destination,
                    log=self.session / "recorder.log",
                    env=self.env,
                    stop=self.stop_requested,
                    captions=captions,
                )
            )
        except (RuntimeError, OSError) as error:
            return str(error)
        if self.process is None:
            self.resources.close()
            return "Session stopped while starting recording."
        return None

    def finish(self) -> str | None:
        if self.process is None:
            return "No recording is active."
        self.close()
        return None

    def close(self) -> None:
        try:
            self.resources.close()
        finally:
            self.process = None


def recording_command(
    session: Path, destination: Path | None, *, captions: bool = True
) -> int:
    state = json.loads((session / "session.json").read_text())
    request = {
        "destination": str(destination.resolve()) if destination else None,
        "captions": captions,
    }
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(Path(state["runtime_directory"]) / "control.sock"))
        connection.sendall((json.dumps(request) + "\n").encode())
        with connection.makefile("r") as response:
            line = response.readline()
    if not line:
        print(
            "Session disconnected before the recording command completed.",
            file=sys.stderr,
        )
        return 1
    error = json.loads(line)["error"]
    if error is not None:
        print(error, file=sys.stderr)
        return 1
    return 0


def handle_recording_command(
    listener: socket.socket, recordings: Recordings, write_state: Callable[[], None]
) -> None:
    try:
        connection, _ = listener.accept()
    except TimeoutError:
        return
    with connection:
        connection.settimeout(2)
        with connection.makefile("r") as request:
            try:
                command = json.loads(request.readline())
                destination = command["destination"]
            except (ValueError, OSError):
                return
        error = (
            recordings.start(Path(destination), captions=command["captions"])
            if destination is not None
            else recordings.finish()
        )
        write_state()
        try:
            connection.sendall((json.dumps({"error": error}) + "\n").encode())
        except BrokenPipeError:
            # The CLI can be interrupted while a recording starts or finalizes.
            pass


def run_session(
    session: Path,
    command: list[str],
    *,
    recording: Path | None = None,
    captions: bool = True,
    size: tuple[int, int] = (1280, 720),
    x11: bool = False,
) -> int:
    if recording is not None:
        recording = recording.resolve()
        error = recording_path_error(session, recording)
        if error is not None:
            raise RuntimeError(error)
    session.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = session / "session.json"
    if state.exists():
        raise RuntimeError(
            f"{state} already exists. Use a different session directory."
        )

    (session / "inputs.jsonl").write_text("", encoding="utf-8")
    stop = Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        # Short paths also fit Wayland and VNC's Unix socket path limits.
        with (
            tempfile.TemporaryDirectory(prefix="framewisp-") as directory,
            ExitStack() as stack,
        ):
            runtime = Path(directory)
            env = session_environment_for_run(runtime)
            compositor = stack.enter_context(
                start_sway(
                    runtime,
                    log=session / "sway.log",
                    env=env,
                    stop=stop,
                    size=size,
                    x11=x11,
                )
            )
            if compositor is None:
                return 0
            sway, display = compositor
            env["WAYLAND_DISPLAY"] = display
            x11_display = None
            app_env = env.copy()
            if x11:
                x11_display = wait_for_x11(
                    runtime, process=sway, log=session / "sway.log", stop=stop
                )
                if x11_display is None:
                    return 0
                app_env.pop("WAYLAND_DISPLAY", None)
                app_env.update(
                    DISPLAY=x11_display,
                    XAUTHORITY="/dev/null",
                    GDK_BACKEND="x11",
                    QT_QPA_PLATFORM="xcb",
                    SDL_VIDEODRIVER="x11",
                )

            vnc = stack.enter_context(
                start_wayvnc(runtime, log=session / "wayvnc.log", env=env, stop=stop)
            )
            if vnc is None:
                return 0
            stack.enter_context(keep_input_devices(runtime))

            backends = {"sway": sway, "wayvnc": vnc}
            recordings = Recordings(session, env, stop, size)
            stack.callback(recordings.close)
            if recording is not None:
                error = recordings.start(recording, captions=captions)
                if error is not None:
                    if stop.is_set():
                        return 0
                    raise RuntimeError(error)

            app = stack.enter_context(
                managed_process(command, log=session / "app.log", env=app_env)
            )
            listener = stack.enter_context(socket.socket(socket.AF_UNIX))
            listener.bind(str(runtime / "control.sock"))
            listener.listen()
            listener.settimeout(0.1)

            def write_state() -> None:
                processes = backends | {"app": app}
                if recordings.process is not None:
                    processes["recorder"] = recordings.process
                temporary = session / ".session.json"
                temporary.write_text(
                    json.dumps(
                        {
                            "runtime_directory": directory,
                            "wayland_display": display,
                            "x11_display": x11_display,
                            "processes": {
                                name: process.pid for name, process in processes.items()
                            },
                        }
                    )
                    + "\n"
                )
                # Keep metadata reads complete while recording commands update it.
                temporary.replace(state)

            write_state()
            print(f"Session ready: {session}", flush=True)
            while not stop.is_set():
                monitored = backends.copy()
                if recordings.process is not None:
                    monitored["recorder"] = recordings.process
                for name, process in monitored.items():
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"{name} exited. See {session / (name + '.log')}"
                        )
                result = app.poll()
                if result is not None:
                    return result if result >= 0 else 128 - result
                handle_recording_command(listener, recordings, write_state)
            return 0
    finally:
        state.unlink(missing_ok=True)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def session_environment(session: Path) -> dict[str, str]:
    state = json.loads((session / "session.json").read_text())
    return os.environ | {
        "XDG_RUNTIME_DIR": state["runtime_directory"],
        "WAYLAND_DISPLAY": state["wayland_display"],
    }


def screenshot(session: Path, destination: Path) -> int:
    return subprocess.run(
        ["grim", "-t", "png", "-o", "HEADLESS-1", str(destination)],
        env=session_environment(session),
        check=False,
        timeout=10,
    ).returncode


def click_pointer(
    session: Path,
    x: int,
    y: int,
    *,
    button: str,
    count: int,
    modifiers: tuple[str, ...] = (),
) -> int:
    arguments = ["move", str(x), str(y)]
    for index in range(count):
        if index:
            arguments.extend(["pause", "0.1"])
        arguments.extend(["click", str(CLICK_BUTTONS[button])])
    return send_input(session, arguments, modifiers=modifiers)


def drag_pointer(
    session: Path,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    duration: float,
    button: str = "left",
    modifiers: tuple[str, ...] = (),
) -> int:
    button_number = str(CLICK_BUTTONS[button])
    arguments = ["move", str(x1), str(y1), "mousedown", button_number]
    steps = max(1, math.ceil(duration * 60))
    for step in range(1, steps + 1):
        if duration:
            arguments.extend(["pause", str(duration / steps)])
        x = round(x1 + (x2 - x1) * step / steps)
        y = round(y1 + (y2 - y1) * step / steps)
        arguments.extend(["move", str(x), str(y)])
    arguments.extend(["mouseup", button_number])
    return send_input(session, arguments, duration=duration, modifiers=modifiers)


def scroll_pointer(session: Path, x: int, y: int, *, direction: str, steps: int) -> int:
    # GTK resolves queued scroll events through their input device. Give it time
    # to consume them before wayvnc removes the pointer on disconnect.
    return send_input(
        session,
        ["move", str(x), str(y)]
        + ["click", str(SCROLL_BUTTONS[direction])] * steps
        + ["pause", "0.1"],
    )


def type_text(session: Path, text: str, *, interval: float) -> int:
    if not text.isascii():
        return type_unicode(session, text, interval=interval)
    arguments: list[str] = []
    for index, character in enumerate(text):
        if index and interval:
            arguments.extend(["pause", str(interval)])
        arguments.extend(["type", character])
    return send_input(session, arguments, duration=max(0, len(text) - 1) * interval)


def type_unicode(session: Path, text: str, *, interval: float) -> int:
    state = json.loads((session / "session.json").read_text())
    if display := state.get("x11_display"):
        env = session_environment(session) | {
            "DISPLAY": display,
            "XAUTHORITY": "/dev/null",
        }
        return type_x11_text(text, interval=interval, env=env)
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
    return subprocess.run(
        arguments,
        env=session_environment(session),
        check=False,
        timeout=15 + duration + len(text) * 0.004,
    ).returncode


def send_input(
    session: Path,
    arguments: list[str],
    *,
    duration: float = 0,
    modifiers: tuple[str, ...] = (),
) -> int:
    env = session_environment(session)
    socket = Path(env["XDG_RUNTIME_DIR"]) / "vnc.sock"
    arguments = (
        [part for modifier in modifiers for part in ("keydown", modifier)]
        + arguments
        + [part for modifier in reversed(modifiers) for part in ("keyup", modifier)]
    )
    # Sway needs time to focus wayvnc's newly created keyboard. Without this,
    # the first character (or a single named key) is lost on each connection.
    return subprocess.run(
        [
            "vncdo",
            "--server",
            str(socket),
            "--timeout",
            str(math.ceil(10 + duration)),
            "--delay",
            "0",
            "--",
            "pause",
            "0.1",
            *arguments,
        ],
        check=False,
        timeout=15 + duration,
    ).returncode

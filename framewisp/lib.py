"""Run one headless Wayland app and control it with existing command-line tools."""

import json
import math
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from string import ascii_lowercase, digits
from threading import Event
from types import FrameType

SWAY_CONFIG = """\
xwayland disable
output HEADLESS-1 mode 1280x720@60Hz
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
    runtime: Path, *, log: Path, env: dict[str, str], stop: Event
) -> Generator[tuple[subprocess.Popen[bytes], str] | None, None, None]:
    """Yield the process and display name, or None if startup is interrupted."""
    config = runtime / "sway.conf"
    config.write_text(SWAY_CONFIG)
    with managed_process(["sway", "-c", str(config)], log=log, env=env) as process:
        display = wait_for_socket(
            runtime, "wayland-*", process=process, log=log, stop=stop
        )
        yield (process, display.name) if display is not None else None


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
    destination: Path, *, log: Path, env: dict[str, str], stop: Event
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
    with managed_process(command, log=log, env=env) as process:
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
        yield process
    if process.returncode != 0:
        raise RuntimeError(f"Recorder failed to finalize {destination}. See {log}")


def run_session(
    session: Path, command: list[str], *, recording: Path | None = None
) -> int:
    if recording is not None:
        recording = recording.resolve()
        reserved = {
            (session / name).resolve()
            for name in (
                "session.json",
                "sway.log",
                "wayvnc.log",
                "recorder.log",
                "app.log",
            )
        }
        if recording in reserved:
            raise RuntimeError(
                f"{recording} is reserved for session files. Choose a new recording path."
            )
        if recording.exists():
            raise RuntimeError(
                f"{recording} already exists. Choose a new recording path."
            )
    session.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = session / "session.json"
    if state.exists():
        raise RuntimeError(
            f"{state} already exists. Use a different session directory."
        )

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
                start_sway(runtime, log=session / "sway.log", env=env, stop=stop)
            )
            if compositor is None:
                return 0
            sway, display = compositor
            env["WAYLAND_DISPLAY"] = display

            vnc = stack.enter_context(
                start_wayvnc(runtime, log=session / "wayvnc.log", env=env, stop=stop)
            )
            if vnc is None:
                return 0

            backends = {"sway": sway, "wayvnc": vnc}
            if recording is not None:
                recorder = stack.enter_context(
                    start_recording(
                        recording, log=session / "recorder.log", env=env, stop=stop
                    )
                )
                if recorder is None:
                    return 0
                backends["recorder"] = recorder

            app = stack.enter_context(
                managed_process(command, log=session / "app.log", env=env)
            )
            state.write_text(
                json.dumps(
                    {
                        "runtime_directory": directory,
                        "wayland_display": display,
                        "processes": {
                            name: process.pid
                            for name, process in (backends | {"app": app}).items()
                        },
                    }
                )
                + "\n"
            )
            print(f"Session ready: {session}", flush=True)
            while not stop.wait(0.1):
                for name, process in backends.items():
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"{name} exited. See {session / (name + '.log')}"
                        )
                result = app.poll()
                if result is not None:
                    return result if result >= 0 else 128 - result
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


def click_pointer(session: Path, x: int, y: int, *, button: str, count: int) -> int:
    arguments = ["move", str(x), str(y)]
    for index in range(count):
        if index:
            arguments.extend(["pause", "0.1"])
        arguments.extend(["click", str(CLICK_BUTTONS[button])])
    return send_input(session, arguments)


def drag_pointer(
    session: Path, x1: int, y1: int, x2: int, y2: int, *, duration: float
) -> int:
    arguments = ["move", str(x1), str(y1), "mousedown", "1"]
    steps = max(1, math.ceil(duration * 60))
    for step in range(1, steps + 1):
        if duration:
            arguments.extend(["pause", str(duration / steps)])
        x = round(x1 + (x2 - x1) * step / steps)
        y = round(y1 + (y2 - y1) * step / steps)
        arguments.extend(["move", str(x), str(y)])
    arguments.extend(["mouseup", "1"])
    return send_input(session, arguments, duration=duration)


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
    arguments: list[str] = []
    for index, character in enumerate(text):
        if index and interval:
            arguments.extend(["pause", str(interval)])
        arguments.extend(["type", character])
    return send_input(session, arguments, duration=max(0, len(text) - 1) * interval)


def send_input(session: Path, arguments: list[str], *, duration: float = 0) -> int:
    env = session_environment(session)
    socket = Path(env["XDG_RUNTIME_DIR"]) / "vnc.sock"
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

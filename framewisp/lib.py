"""Run one headless Wayland app and control it with existing command-line tools."""

import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Event
from types import FrameType

SWAY_CONFIG = """\
xwayland disable
output HEADLESS-1 mode 1280x720@60Hz
seat seat0 fallback true
input * xkb_layout us
default_border none
"""

KEYS = {"Return": "enter", "Tab": "tab", "BackSpace": "bsp"}


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


def run_session(session: Path, command: list[str]) -> int:
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
            config = runtime / "sway.conf"
            config.write_text(SWAY_CONFIG)
            env = os.environ.copy()
            for key in (
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "SWAYSOCK",
                "DBUS_SESSION_BUS_ADDRESS",
            ):
                env.pop(key, None)
            env.update(
                XDG_RUNTIME_DIR=directory,
                WLR_BACKENDS="headless",
                WLR_RENDERER="pixman",
                WLR_LIBINPUT_NO_DEVICES="1",
                GDK_BACKEND="wayland",
                GSK_RENDERER="cairo",
            )
            sway = stack.enter_context(
                managed_process(
                    ["sway", "-c", str(config)], log=session / "sway.log", env=env
                )
            )
            display = wait_for_socket(
                runtime, "wayland-*", process=sway, log=session / "sway.log", stop=stop
            )
            if display is None:
                return 0
            env["WAYLAND_DISPLAY"] = display.name

            vnc = stack.enter_context(
                managed_process(
                    [
                        "wayvnc",
                        "-C",
                        "/dev/null",
                        "-k",
                        "us",
                        f"unix:{runtime / 'vnc.sock'}",
                    ],
                    log=session / "wayvnc.log",
                    env=env,
                )
            )
            if (
                wait_for_socket(
                    runtime,
                    "vnc.sock",
                    process=vnc,
                    log=session / "wayvnc.log",
                    stop=stop,
                )
                is None
            ):
                return 0

            app = stack.enter_context(
                managed_process(command, log=session / "app.log", env=env)
            )
            state.write_text(
                json.dumps(
                    {
                        "runtime_directory": directory,
                        "wayland_display": display.name,
                        "processes": {
                            "sway": sway.pid,
                            "wayvnc": vnc.pid,
                            "app": app.pid,
                        },
                    }
                )
                + "\n"
            )
            print(f"Session ready: {session}", flush=True)
            while not stop.wait(0.1):
                for name, process in (("sway", sway), ("wayvnc", vnc)):
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
        timeout=10,
    ).returncode


def send_input(session: Path, arguments: list[str]) -> int:
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
            "10",
            "--",
            "pause",
            "0.1",
            *arguments,
        ],
        timeout=15,
    ).returncode

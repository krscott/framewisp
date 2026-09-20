"""Run one headless app and control it with existing command-line tools."""

import json
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
from threading import Event
from types import FrameType
from typing import BinaryIO, cast

from framewisp.actions import InputAction
from framewisp.batch import MAX_REQUEST_BYTES, Batch
from framewisp.captions import capture_origin, recorder_output, render_captions
from framewisp.connection import reply as reply
from framewisp.errors import (
    SOCKET_ACCESS_HINT,
    SessionError,
    display_command,
    log_failure,
)
from framewisp.inputs import VNC, InputWorker
from framewisp.keys import key_commands as key_commands

SWAY_CONFIG = """\
xwayland disable
output HEADLESS-1 mode {width}x{height}@60Hz
seat seat0 fallback true
input * xkb_layout us
default_border none
"""


@contextmanager
def managed_process(
    command: list[str],
    *,
    log: Path,
    env: dict[str, str],
    output: BinaryIO | None = None,
) -> Generator[subprocess.Popen[bytes], None, None]:
    with ExitStack() as stack:
        destination = (
            output if output is not None else stack.enter_context(log.open("wb"))
        )
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=destination,
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
            raise log_failure(
                f"Process exited while starting display socket {runtime / pattern}",
                log,
                display=True,
            )
        for path in runtime.glob(pattern):
            if path.is_socket():
                return path
        if time.monotonic() >= deadline:
            raise log_failure(
                f"Waiting for display socket {runtime / pattern} timed out",
                log,
                display=True,
            )
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
        "DBUS_STARTER_ADDRESS",
        "DBUS_STARTER_BUS_TYPE",
        "AT_SPI_BUS_ADDRESS",
        "GTK_A11Y",
        "NO_AT_BRIDGE",
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


def start_inspection_buses(
    stack: ExitStack, runtime: Path, session: Path, env: dict[str, str], stop: Event
) -> dict[str, subprocess.Popen[bytes]] | None:
    """Own both buses and the registry; never activate host desktop services."""
    config = runtime / "bus.conf"
    config.write_text(
        "<busconfig><type>session</type><auth>EXTERNAL</auth><listen>unix:tmpdir=/tmp</listen>"
        '<policy context="default"><allow send_destination="*"/>'
        '<allow receive_sender="*"/><allow own="*"/></policy>'
        '<limit name="max_message_size">1048576</limit></busconfig>'
    )
    processes: dict[str, subprocess.Popen[bytes]] = {}
    for name, variable in (
        ("dbus", "DBUS_SESSION_BUS_ADDRESS"),
        ("accessibility", "AT_SPI_BUS_ADDRESS"),
    ):
        address = f"unix:path={runtime / (name + '.sock')}"
        log = session / (name + ".log")
        process = stack.enter_context(
            managed_process(
                [
                    "dbus-daemon",
                    "--nofork",
                    f"--config-file={config}",
                    f"--address={address}",
                ],
                log=log,
                env=env,
            )
        )
        processes[name] = process
        if (
            wait_for_socket(
                runtime, name + ".sock", process=process, log=log, stop=stop
            )
            is None
        ):
            return None
        env[variable] = address
    env.update(GTK_A11Y="atspi", QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1")
    registry = os.environ.get("FRAMEWISP_ATSPI_REGISTRY", "at-spi2-registryd")
    processes["registry"] = stack.enter_context(
        managed_process([registry], log=session / "registry.log", env=env)
    )
    # Wait for the registry's bus name before launching apps that register with it.
    from framewisp.inspection import wait_for_registry

    wait_for_registry(env["AT_SPI_BUS_ADDRESS"], stop)
    return processes


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
            raise log_failure("Compositor exited during X11 startup", log, display=True)
        if path.exists() and (display := path.read_text()):
            return display
        if time.monotonic() >= deadline:
            raise log_failure("Waiting for Xwayland timed out", log, display=True)
        stop.wait(0.05)
    return None


@contextmanager
def keep_input_devices(runtime: Path, stop: Event) -> Generator[VNC, None, None]:
    """Own one input connection. Connection loss stops access without replay."""
    # Only the runner needs Twisted; loading it in every control CLI adds latency.
    from vncdotool import api
    from vncdotool.client import VNCDoToolFactory

    lost = Event()

    class Factory(VNCDoToolFactory):  # type: ignore[misc]
        def clientConnectionLost(self, connector: object, reason: object) -> None:
            if not stop.is_set():
                lost.set()
            stop.set()

    try:
        with api.connect(
            str(runtime / "vnc.sock"), factory_class=Factory, timeout=10
        ) as connection:
            connection.pause(0.1)
            # A stalled reactor call must not hold up cancellation indefinitely.
            connection.timeout = 1
            yield cast(VNC, connection)
            if lost.is_set():
                raise SessionError(
                    "Persistent VNC connection lost. Session stopped without replaying input."
                )
    finally:
        api.shutdown()


@contextmanager
def start_wayvnc(
    runtime: Path, *, log: Path, env: dict[str, str], stop: Event
) -> Generator[subprocess.Popen[bytes] | None, None, None]:
    """Yield the ready process, or None if startup is interrupted."""
    command = [
        "wayvnc",
        "--disable-clipboard",
        "-C",
        "/dev/null",
        "-k",
        "us",
        f"unix:{runtime / 'vnc.sock'}",
    ]
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
    with (
        recorder_output(log) as output,
        managed_process(command, log=log, env=recorder_env, output=output) as process,
    ):
        deadline = time.monotonic() + 10
        origin = 0.0
        while not stop.is_set():
            if process.poll() is not None:
                raise log_failure("Recorder exited during startup", log)
            # wf-recorder opens and writes the MP4 header after receiving a frame.
            if destination.exists() and destination.stat().st_size > 0:
                if not captions:
                    break
                try:
                    origin = capture_origin(log)
                    break
                except RuntimeError:
                    # The output reader may still be flushing the first frame event.
                    pass
            if time.monotonic() >= deadline:
                raise log_failure("Waiting for recording to start timed out", log)
            stop.wait(0.05)
        else:
            yield None
            return
        try:
            yield process
        finally:
            stopped = time.monotonic()
    if process.returncode != 0:
        raise log_failure(f"Recorder failed to finalize {destination}", log)
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
            "dbus.log",
            "accessibility.log",
            "registry.log",
        )
    }
    if destination in reserved:
        return (
            f"{destination} is reserved for session files. Choose a new recording path."
        )
    if destination.exists():
        return f"{destination} already exists. Choose a new recording path."
    return None


def recording_summary(destination: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise SessionError(
            f"Could not inspect recording {destination}: {result.stderr.strip()}"
        )
    data = json.loads(result.stdout)
    return {
        "path": str(destination),
        "duration_seconds": float(data["format"]["duration"]),
        "width": data["streams"][0]["width"],
        "height": data["streams"][0]["height"],
        "size_bytes": destination.stat().st_size,
    }


@dataclass
class Recordings:
    session: Path
    env: dict[str, str]
    stop_requested: Event
    size: tuple[int, int]
    process: subprocess.Popen[bytes] | None = None
    resources: ExitStack = field(default_factory=ExitStack)
    destination: Path | None = None
    last_summary: dict[str, object] | None = None

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
        self.destination = destination
        return None

    def finish(self) -> str | None:
        if self.process is None:
            return "No recording is active."
        self.close()
        return None

    def close(self) -> None:
        try:
            self.resources.close()
            if self.destination is not None:
                self.last_summary = recording_summary(self.destination)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(str(error)) from None
        finally:
            self.process = None
            self.destination = None


def session_command(
    session: Path,
    action: str,
    destination: Path | None = None,
    *,
    captions: bool = True,
    parameters: dict[str, object] | None = None,
) -> int:
    state = json.loads((session / "session.json").read_text())
    if state.get("control_protocol") != 1:
        raise SessionError(
            "This session uses an older or unsupported control protocol. "
            "Stop its original runner with Ctrl+C or SIGTERM, then start a new session "
            "with this version of framewisp."
        )
    request = {
        "action": action,
        "session": str(session),
        "destination": str(destination.resolve()) if destination else None,
        "captions": captions,
        "parameters": parameters,
    }
    if parameters is not None and state.get("persistent_input") is not True:
        raise SessionError(
            "This runner does not support persistent input. Restart the session with this version of framewisp."
        )
    if action == "batch" and state.get("batch_input") is not True:
        raise SessionError(
            "This runner does not support batches. Restart the session with this version of framewisp."
        )
    message = (json.dumps(request) + "\n").encode()
    if len(message) > MAX_REQUEST_BYTES:
        raise SessionError("Session request exceeds the 1 MiB limit.")
    control = Path(state["runtime_directory"]) / "control.sock"
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(control))
            connection.sendall(message)
            with connection.makefile("r") as response:
                line = response.readline()
    except OSError as connection_error:
        raise SessionError(
            f"Cannot reach session {session} at {control}: {connection_error}\n{SOCKET_ACCESS_HINT}"
        ) from None
    if not line:
        print(
            f"Session {session} disconnected before {action} completed.",
            file=sys.stderr,
        )
        return 1
    result = json.loads(line)
    error = result["error"]
    if result["data"] is not None and (error is None or action == "batch"):
        print(json.dumps(result["data"]))
    if error is not None:
        print(error, file=sys.stderr)
        return 1
    return 0


def handle_session_command(
    listener: socket.socket,
    recordings: Recordings,
    write_state: Callable[[], None],
    status: Callable[[], dict[str, object]],
    inputs: InputWorker,
) -> socket.socket | None:
    """Return a stop caller's connection for the runner to reply after cleanup."""
    try:
        connection, _ = listener.accept()
    except TimeoutError:
        return None
    stopping = False
    try:
        connection.settimeout(2)
        with connection.makefile("rb") as request:
            try:
                line = request.readline(MAX_REQUEST_BYTES + 1)
                if len(line) > MAX_REQUEST_BYTES:
                    reply(connection, error="Session request exceeds the 1 MiB limit.")
                    return None
                raw: object = json.loads(line)
            except (ValueError, OSError, RecursionError):
                return None
        if not isinstance(raw, dict):
            reply(connection, error="Expected a session command object.")
            return None
        command = cast(dict[str, object], raw)
        if command.get("session") != str(recordings.session):
            reply(
                connection,
                error="Session metadata points to a different session. Use the original session directory.",
            )
            return None
        action = command.get("action")
        if not isinstance(action, str):
            reply(connection, error="action must be a string.")
            return None
        error = None
        data = None
        if action == "stop":
            stopping = True
            return connection
        if action in {"move", "click", "drag", "scroll", "type", "key", "batch"}:
            try:
                input_action: InputAction | Batch
                if action == "batch":
                    input_action = Batch.parse(command.get("parameters"))
                    for capture in (input_action.capture, input_action.failure_capture):
                        if capture is None:
                            continue
                        if not capture.path.parent.is_dir():
                            raise ValueError(
                                "capture.path requires an existing parent directory."
                            )
                        path_error = recording_path_error(
                            recordings.session, capture.path.resolve()
                        )
                        if path_error is not None:
                            raise ValueError(
                                path_error.replace("recording path", "capture path")
                            )
                else:
                    input_action = InputAction.parse(action, command.get("parameters"))
            except (ValueError, TypeError, OverflowError, OSError) as failure:
                reply(connection, error=str(failure))
                return None
            if inputs.submit(connection, input_action):
                # The worker now owns the socket until completion or cancellation.
                stopping = True
            else:
                reply(
                    connection,
                    error="Input queue is full. Wait for pending actions to finish.",
                )
            return None
        if action == "status":
            data = status()
        elif action == "record-start":
            destination = command.get("destination")
            captions = command.get("captions", True)
            if not isinstance(destination, str):
                error = "record-start requires a destination"
            elif not isinstance(captions, bool):
                error = "captions must be true or false"
            else:
                error = recordings.start(Path(destination), captions=captions)
        elif action == "record-stop":
            error = recordings.finish()
            if error is None:
                data = recordings.last_summary
        else:
            error = "Unsupported headless session command."
        write_state()
        reply(connection, error=error, data=data)
    finally:
        if not stopping:
            connection.close()
    return None


def run_session(
    session: Path,
    command: list[str],
    *,
    recording: Path | None = None,
    captions: bool = True,
    size: tuple[int, int] = (1280, 720),
    x11: bool = False,
) -> int:
    session = session.resolve()
    if recording is not None:
        recording = recording.resolve()
        error = recording_path_error(session, recording)
        if error is not None:
            raise SessionError(error)
    session.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = session / "session.json"
    if state.exists():
        raise SessionError(
            f"{state} already exists. Use a different session directory."
        )

    (session / "inputs.jsonl").write_text("", encoding="utf-8")
    stop = Event()
    stop_connection: socket.socket | None = None
    shutdown_error: str | None = None
    recordings: Recordings | None = None

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
            buses = start_inspection_buses(stack, runtime, session, env, stop)
            if buses is None:
                return 0
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
            input_client = stack.enter_context(keep_input_devices(runtime, stop))

            backends = buses | {"sway": sway, "wayvnc": vnc}
            recordings = Recordings(session, env, stop, size)
            stack.callback(recordings.close)
            if recording is not None:
                error = recordings.start(recording, captions=captions)
                if error is not None:
                    if stop.is_set():
                        return 0
                    raise SessionError(error)

            app = stack.enter_context(
                managed_process(command, log=session / "app.log", env=app_env)
            )
            listener = stack.enter_context(socket.socket(socket.AF_UNIX))
            listener.bind(str(runtime / "control.sock"))
            listener.listen()
            listener.settimeout(0.1)
            inputs = InputWorker(
                session,
                input_client,
                stop,
                x11=x11,
                capture=lambda path, cancelled: batch_screenshot(
                    session, path, cancelled
                ),
            )
            stack.callback(inputs.close)

            def write_state() -> None:
                assert recordings is not None
                processes = backends | {"app": app}
                if recordings.process is not None:
                    processes["recorder"] = recordings.process
                temporary = session / ".session.json"
                temporary.write_text(
                    json.dumps(
                        {
                            "control_protocol": 1,
                            "persistent_input": True,
                            "inspection_protocol": 1,
                            "accessibility_bus": env["AT_SPI_BUS_ADDRESS"],
                            "batch_input": True,
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

            def status() -> dict[str, object]:
                assert recordings is not None
                return {
                    "session": str(session),
                    "status": "running",
                    "backend": "x11" if x11 else "wayland",
                    "wayland_display": display,
                    "x11_display": x11_display,
                    "width": size[0],
                    "height": size[1],
                    "app": {"pid": app.pid, "running": app.poll() is None},
                    "recording": (
                        {
                            "path": str(recordings.destination),
                            "pid": recordings.process.pid,
                            "running": recordings.process.poll() is None,
                        }
                        if recordings.process is not None
                        else None
                    ),
                }

            write_state()
            print(f"Session ready: {session}", flush=True)
            while not stop.is_set():
                monitored = backends.copy()
                if recordings.process is not None:
                    monitored["recorder"] = recordings.process
                for name, process in monitored.items():
                    if process.poll() is not None:
                        raise log_failure(f"{name} exited", session / (name + ".log"))
                result = app.poll()
                if result is not None:
                    return result if result >= 0 else 128 - result
                stop_connection = handle_session_command(
                    listener, recordings, write_state, status, inputs
                )
                if stop_connection is not None:
                    stop.set()
            return 0
    except BaseException as error:
        # A stop caller must not receive success when shutdown or encoding failed.
        shutdown_error = str(error)
        raise
    finally:
        state.unlink(missing_ok=True)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if stop_connection is not None:
            with stop_connection:
                reply(
                    stop_connection,
                    error=shutdown_error,
                    data={
                        "session": str(session),
                        "status": "stopped",
                        "recording": recordings.last_summary if recordings else None,
                    },
                )


def session_environment(session: Path) -> dict[str, str]:
    state = json.loads((session / "session.json").read_text())
    return os.environ | {
        "XDG_RUNTIME_DIR": state["runtime_directory"],
        "WAYLAND_DISPLAY": state["wayland_display"],
    }


def screenshot(
    session: Path, destination: Path, *, cancelled: Callable[[], bool] | None = None
) -> int:
    return display_command(
        session,
        ["grim", "-t", "png", "-o", "HEADLESS-1", str(destination)],
        env=session_environment(session),
        timeout=10,
        cancelled=cancelled,
    )


def batch_screenshot(
    session: Path, destination: Path, cancelled: Callable[[], bool]
) -> int:
    """Publish only a complete PNG, without overwriting another client's artifact."""
    try:
        with tempfile.TemporaryDirectory(
            prefix=".framewisp-capture-", dir=destination.parent
        ) as directory:
            temporary = Path(directory) / "capture.png"
            screenshot(session, temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError(
                    "Capture cancelled; caller disconnected or session stopped."
                )
            destination.hardlink_to(temporary)
    except InterruptedError:
        raise
    except OSError as error:
        raise SessionError(f"Cannot save capture to {destination}: {error}") from None
    return 0

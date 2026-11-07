"""Run one headless app and control it with existing command-line tools."""

import json
import os
import shlex
import shutil
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
from typing import Any, Literal, cast, get_args

from framewisp.actions import InputAction
from framewisp.batch import MAX_REQUEST_BYTES, Batch
from framewisp.captions import (
    CAPTION_MODES,
    DEFAULT_CAPTIONS,
    CaptionMode,
    capture_origin,
    recorder_output,
    render_recording,
)
from framewisp.connection import (
    capture_path_error,
    parse_command,
    receive_request,
    reply,
    resolved_request_path,
)
from framewisp.console import ConsoleCapture, app_output
from framewisp.environment import (
    caller_environment,
    sanitized_environment,
    unix_bus_address,
)
from framewisp.errors import (
    SOCKET_ACCESS_HINT,
    SessionError,
    app_exit_message,
    display_command,
    log_failure,
)
from framewisp.inputs import VNC, InputWorker
from framewisp.keys import key_commands as key_commands
from framewisp.layout import ConsolePanel, VideoLayout, video_layout
from framewisp.ownership import (
    ACTIVE_RECORDING,
    headless_runtime,
    kept_recording,
    refuse_abandoned_ownership,
    session_lease,
)
from framewisp.processes import OwnedProcess, managed_process

SWAY_CONFIG = """\
xwayland disable
output HEADLESS-1 mode {width}x{height}@60Hz
output HEADLESS-1 scale 1
seat seat0 fallback true
input * xkb_layout us
default_border none
"""


def wait_for_socket(
    runtime: Path,
    pattern: str,
    *,
    process: OwnedProcess,
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


def session_environment_for_run(
    runtime: Path, source: dict[str, str]
) -> dict[str, str]:
    env = source.copy()
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
    env.setdefault("ACCESSIBILITY_ENABLED", "1")
    return env


def app_command(command: list[str], env: dict[str, str]) -> list[str]:
    """Make the bundled demo available to nix run without changing the app PATH."""
    if (
        command[0] == "framewisp-demo"
        and shutil.which(
            "framewisp-demo", path=env.get("PATH", os.defpath) or os.curdir
        )
        is None
    ):
        if demo := shutil.which("framewisp-demo"):
            return [demo, *command[1:]]
    return command


def start_inspection_buses(
    stack: ExitStack,
    runtime: Path,
    session: Path,
    env: dict[str, str],
    app_env: dict[str, str],
    stop: Event,
) -> dict[str, OwnedProcess] | None:
    """Own both buses and the registry; never activate host desktop services."""
    config = runtime / "bus.conf"
    config.write_text(
        "<busconfig><type>session</type><auth>EXTERNAL</auth><listen>unix:tmpdir=/tmp</listen>"
        '<policy context="default"><allow send_destination="*"/>'
        '<allow receive_sender="*"/><allow own="*"/></policy>'
        '<limit name="max_message_size">1048576</limit></busconfig>'
    )
    processes: dict[str, OwnedProcess] = {}
    for name, variable in (
        ("dbus", "DBUS_SESSION_BUS_ADDRESS"),
        ("accessibility", "AT_SPI_BUS_ADDRESS"),
    ):
        address = unix_bus_address(runtime / (name + ".sock"))
        log = session / (name + ".log")
        process = stack.enter_context(
            managed_process(
                [
                    shutil.which("dbus-daemon") or "dbus-daemon",
                    "--nofork",
                    f"--config-file={config}",
                    f"--address={address}",
                ],
                log=log,
                env=app_env,
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
        app_env[variable] = address
    env.update(GTK_A11Y="atspi", QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1")
    app_env.update(GTK_A11Y="atspi", QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1")
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
) -> Generator[tuple[OwnedProcess, str] | None, None, None]:
    """Yield the process and display name, or None if startup is interrupted."""
    config = runtime / "sway.conf"
    contents = SWAY_CONFIG.format(width=size[0], height=size[1])
    if x11:
        # Sway chooses a free X display. Read its environment from a child,
        # rather than guessing a number or inheriting the host desktop's DISPLAY.
        probe = shlex.join(
            [
                sys.executable,
                "-I",
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
    runtime: Path, *, process: OwnedProcess, log: Path, stop: Event
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
) -> Generator[OwnedProcess | None, None, None]:
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
    destination: Path,
    *,
    log: Path,
    env: dict[str, str],
    stop: Event,
    captions: CaptionMode,
    click_pings: bool,
    console: ConsoleCapture,
    compact: bool = False,
    layout: VideoLayout,
) -> Generator[OwnedProcess | None, None, None]:
    """Wait for the MP4 header before yielding; finalize while Sway is still alive.

    Everything up to publishing works on a private clip, so nothing but
    `publish_recording` can touch the destination.
    """
    clip = private_clip(destination)
    active = log.parent / ACTIVE_RECORDING
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
        str(clip),
    ]
    timed = captions != "none" or click_pings or layout.panel is not None or compact
    recorder_env = env | {"WAYLAND_DEBUG": "client"} if timed else env
    capturing = False
    try:
        active.unlink(missing_ok=True)
        active.symlink_to(clip)
        with (
            recorder_output(log) as output,
            managed_process(
                command, log=log, env=recorder_env, output=output
            ) as process,
        ):
            deadline = time.monotonic() + 10
            origin = 0.0
            while not stop.is_set():
                if process.poll() is not None:
                    raise log_failure("Recorder exited during startup", log)
                # wf-recorder opens and writes the MP4 header after receiving a frame.
                if clip.exists() and clip.stat().st_size > 0:
                    if not timed:
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
            capturing = True
            try:
                yield process
            finally:
                stopped = time.monotonic()
        if process.returncode != 0:
            raise log_failure(f"Recorder failed to finalize {destination}", log)
        failure = None
        try:
            # A clip with no duration has no video to render on (R-REC-22).
            if restore_hidden_frames(clip, log=log) and timed:
                render_recording(
                    clip,
                    input_log=log.parent / "inputs.jsonl",
                    origin=origin,
                    stopped=stopped,
                    log=log.parent / "captions.log",
                    captions=captions,
                    click_pings=click_pings,
                    console=console,
                    compact=compact,
                    layout=layout,
                )
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            failure = error
        publish_recording(clip, destination, failure)
    finally:
        if not capturing:
            shutil.rmtree(clip.parent, ignore_errors=True)
        # A clip that remains stays linked, in case a later error hides its path.
        if not clip.exists():
            active.unlink(missing_ok=True)


def private_clip(destination: Path) -> Path:
    """Return an unused clip path that can later be linked to `destination`."""
    try:
        directory = Path(
            tempfile.mkdtemp(prefix=".framewisp-recording-", dir=destination.parent)
        )
    except OSError as error:
        raise RuntimeError(f"Cannot record to {destination}: {error}") from None
    try:
        probe_destination(directory, destination)
    except RuntimeError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory / "recording.mp4"


def probe_destination(directory: Path, destination: Path) -> None:
    """Fail now on anything that would otherwise stop publishing the finished clip."""
    names = directory / "names"
    probe = names / destination.name
    link = directory / "link"
    try:
        names.mkdir()
        probe.touch()
        try:
            link.hardlink_to(probe)
        except OSError as error:
            raise RuntimeError(
                f"Cannot record to {destination}: its filesystem must support hard "
                f"links, so a recording can never replace an existing file ({error}). "
                "Choose a path on another filesystem."
            ) from None
        link.unlink()
        shutil.rmtree(names)
    except OSError as error:
        raise RuntimeError(f"Cannot record to {destination}: {error}") from None


def publish_recording(clip: Path, destination: Path, failure: Exception | None) -> None:
    """Link a clip into place, which fails rather than replaces a file.

    Report `failure` too, naming wherever the clip ended up.
    """
    try:
        destination.hardlink_to(clip)
    except OSError as error:
        cause = f" {failure}" if failure is not None else ""
        raise RuntimeError(
            f"Cannot save recording to {destination}: {error}.{cause}"
        ) from None
    shutil.rmtree(clip.parent, ignore_errors=True)
    if failure is not None:
        raise RuntimeError(str(failure).replace(str(clip), str(destination)))


def recording_path_error(session: Path, destination: Path) -> str | None:
    reserved = {
        (session / name).resolve()
        for name in (
            ".headless.lock",
            ".headless-owner.json",
            ".headless-owner.tmp",
            ACTIVE_RECORDING,
            "session.json",
            ".session.json",
            "app-exit.json",
            ".app-exit.json",
            "sway.log",
            "wayvnc.log",
            "recorder.log",
            "app.log",
            "console.jsonl",
            "inputs.jsonl",
            "activity.jsonl",
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


@dataclass(frozen=True)
class ClipProbe:
    """What ffprobe measured. A clip can have no stream or container duration."""

    duration: float | None
    width: int | None
    height: int | None


def probe_clip(path: Path) -> ClipProbe:
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
            str(path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise SessionError(
            f"Could not inspect recording {path}: {result.stderr.strip()}"
        )
    data = cast(dict[str, Any], json.loads(result.stdout))
    duration = data["format"].get("duration")
    streams = cast(list[dict[str, Any]], data["streams"])
    stream = streams[0] if streams else {}
    return ClipProbe(
        duration=float(duration) if duration is not None else None,
        width=stream.get("width"),
        height=stream.get("height"),
    )


def restore_hidden_frames(clip: Path, *, log: Path) -> bool:
    """Remux a clip whose edit list hides its frames (R-REC-3).

    Return whether the clip has a duration. A failed remux is logged because the
    clip stays usable as evidence, and its summary reports the null (R-REC-22).
    """
    probe = probe_clip(clip)
    if probe.duration is not None:
        return True
    if probe.width is None:
        return False
    remuxed = clip.with_name("remuxed.mp4")
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ignore_editlist",
                "1",
                "-i",
                str(clip),
                "-c",
                "copy",
                str(remuxed),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=10,
        )
        problem = result.stderr.strip() if result.returncode else None
    except subprocess.TimeoutExpired as error:
        problem = str(error)
    if problem is not None:
        remuxed.unlink(missing_ok=True)
        try:
            with log.open("a", errors="backslashreplace") as output:
                output.write(f"Could not restore frames in {clip}: {problem}\n")
        except OSError:
            pass  # The disk that failed the remux can also refuse this note.
        return False
    remuxed.replace(clip)
    return probe_clip(clip).duration is not None


def recording_summary(destination: Path) -> dict[str, object]:
    probe = probe_clip(destination)
    return {
        "path": str(destination),
        "duration_seconds": probe.duration,
        "width": probe.width,
        "height": probe.height,
        "size_bytes": destination.stat().st_size,
    }


@dataclass
class Recordings:
    session: Path
    env: dict[str, str]
    stop_requested: Event
    size: tuple[int, int]
    console_capture: ConsoleCapture
    process: OwnedProcess | None = None
    resources: ExitStack = field(default_factory=ExitStack)
    destination: Path | None = None
    last_summary: dict[str, object] | None = None

    def start(
        self,
        destination: Path,
        *,
        captions: CaptionMode = DEFAULT_CAPTIONS,
        click_pings: bool = True,
        console: ConsolePanel | None = None,
        compact: bool = False,
    ) -> str | None:
        if self.process is not None:
            return "A recording is already active. Use record-stop first."
        if any(dimension % 2 for dimension in self.size):
            return "Recording requires even display width and height."
        layout = video_layout(self.size, console)
        if isinstance(layout, str):
            return layout
        try:
            error = recording_path_error(self.session, destination)
            if error is not None:
                return error
            self.process = self.resources.enter_context(
                start_recording(
                    destination,
                    log=self.session / "recorder.log",
                    env=self.env,
                    stop=self.stop_requested,
                    captions=captions,
                    click_pings=click_pings,
                    console=self.console_capture,
                    compact=compact,
                    layout=layout,
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
            raise SessionError(self.with_kept_clip(str(error))) from None
        finally:
            self.process = None
            self.destination = None

    def with_kept_clip(self, error: str) -> str:
        clip = kept_recording(self.session)
        return f"{error}\nThe clip remains at {clip}." if clip is not None else error


def session_command(
    session: Path,
    action: str,
    destination: Path | None = None,
    *,
    captions: CaptionMode = DEFAULT_CAPTIONS,
    click_pings: bool = True,
    console: ConsolePanel | None = None,
    compact: bool = False,
    parameters: dict[str, object] | None = None,
) -> int:
    state = json.loads((session / "session.json").read_text())
    if state.get("control_protocol") != 1:
        raise SessionError(
            "This session uses an older or unsupported control protocol. "
            "Stop its original runner with Ctrl+C or SIGTERM, then start a new session "
            "with this version of framewisp."
        )
    if action == "record-start" and state.get("caption_modes") is not True:
        raise SessionError(
            "This runner does not support caption modes. Restart the session "
            "with this version of framewisp."
        )
    if console is not None and state.get("console_panels") is not True:
        raise SessionError(
            "This runner does not support console panel options. Restart the session "
            "with this version of framewisp."
        )
    if compact and state.get("compact_recording") is not True:
        raise SessionError(
            "This runner does not support compact recordings. Restart the session "
            "with this version of framewisp."
        )
    if (
        action == "record-start"
        and click_pings
        and state.get("click_pings_recording") is not True
    ):
        raise SessionError(
            "This runner does not support click pings. Restart the session with "
            "this version of framewisp, or pass --no-click-pings."
        )
    request: dict[str, object] = {
        "action": action,
        "session": str(session),
        "destination": str(destination.resolve()) if destination else None,
        "captions": captions,
        "click_pings": click_pings,
        "compact": compact,
        "parameters": parameters,
    }
    # An older runner rejects this key even when it is null.
    if console is not None:
        request["console"] = console.parameters()
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


@dataclass(frozen=True)
class RecordStart:
    destination: Path
    captions: CaptionMode
    click_pings: bool
    console: ConsolePanel | None
    compact: bool

    @staticmethod
    def parse(command: dict[str, object]) -> "RecordStart":
        console = ConsolePanel.parse(command.get("console"))
        destination = command.get("destination")
        captions = command.get("captions")
        click_pings = command.get("click_pings", True)
        compact = command.get("compact", False)
        if not isinstance(destination, str):
            raise ValueError("record-start requires a destination")
        if captions not in CAPTION_MODES:
            raise ValueError(f"captions must be one of {', '.join(CAPTION_MODES)}")
        if not isinstance(click_pings, bool):
            raise ValueError("click_pings must be true or false")
        if not isinstance(compact, bool):
            raise ValueError("compact must be true or false")
        path = Path(destination)
        if not path.is_absolute():
            raise ValueError("destination must be absolute in runner requests.")
        return RecordStart(
            resolved_request_path(path, "destination"),
            captions,
            click_pings,
            console,
            compact,
        )


SessionAction = Literal["stop", "status", "record-stop"]
SESSION_ACTIONS: tuple[SessionAction, ...] = get_args(SessionAction)
ControlRequest = SessionAction | RecordStart | InputAction | Batch


def parse_batch(raw: object, recordings: Recordings) -> Batch:
    batch = Batch.parse(raw)
    for capture in (batch.capture, batch.failure_capture):
        if capture is None:
            continue
        if not capture.path.parent.is_dir():
            raise ValueError("capture.path requires an existing parent directory.")
        resolved = resolved_request_path(capture.path, "capture.path")
        path_error = recording_path_error(recordings.session, resolved)
        if path_error is not None:
            raise ValueError(path_error.replace("recording path", "capture path"))
        error = capture_path_error(resolved, recordings.destination)
        if error is not None:
            raise ValueError(error)
    return batch


def parse_request(line: bytes, recordings: Recordings) -> ControlRequest:
    """Validate one request line, changing no session state."""
    command = parse_command(line)
    if command.get("session") != str(recordings.session):
        raise ValueError(
            "Session metadata points to a different session. Use the original session directory."
        )
    action = command.get("action")
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    if action in SESSION_ACTIONS:
        return action
    if action == "record-start":
        return RecordStart.parse(command)
    if action == "batch":
        return parse_batch(command.get("parameters"), recordings)
    if action in {"move", "click", "drag", "scroll", "type", "key"}:
        return InputAction.parse(action, command.get("parameters"))
    raise ValueError("Unsupported headless session command.")


def handle_session_command(
    listener: socket.socket,
    recordings: Recordings,
    write_state: Callable[[], None],
    status: Callable[[], dict[str, object]],
    inputs: InputWorker,
) -> socket.socket | None:
    """Return a stop caller's connection for the runner to reply after cleanup.

    Execution is not guarded: an unexpected error in it is a runner failure and
    ends the session.
    """
    try:
        connection, _ = listener.accept()
    except TimeoutError:
        return None
    stopping = False
    try:
        request: ControlRequest | None = receive_request(
            connection,
            lambda failure: reply(connection, error=failure),
            lambda line: parse_request(line, recordings),
        )
        if request is None:
            return None
        if request == "stop":
            stopping = True
            return connection
        if isinstance(request, (InputAction, Batch)):
            if inputs.submit(connection, request):
                # The worker now owns the socket until completion or cancellation.
                stopping = True
            else:
                reply(
                    connection,
                    error="Input queue is full. Wait for pending actions to finish.",
                )
            return None
        error = None
        data = None
        if request == "status":
            data = status()
        elif request == "record-stop":
            try:
                error = recordings.finish()
            except SessionError as failure:
                # The session still fails; the caller needs the reason and raw path.
                reply(connection, error=str(failure))
                raise
            if error is None:
                data = recordings.last_summary
        else:
            error = recordings.start(
                request.destination,
                captions=request.captions,
                click_pings=request.click_pings,
                console=request.console,
                compact=request.compact,
            )
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
    captions: CaptionMode = DEFAULT_CAPTIONS,
    click_pings: bool = True,
    console: ConsolePanel | None = None,
    compact: bool = False,
    size: tuple[int, int] = (1280, 720),
    x11: bool = False,
    retain_input_content: bool = False,
) -> int:
    fonts = os.environ.get("FRAMEWISP_FONTCONFIG_FILE")
    if not fonts or not Path(fonts).is_file():
        raise SessionError(
            "run requires FRAMEWISP_FONTCONFIG_FILE, a Fontconfig file that selects "
            "a fixed set of fonts. Use the Nix package or nix develop, or set it to "
            "your own file. Pointing it at the host's fonts.conf works, but output "
            "can then differ between hosts."
        )
    session = session.resolve()
    if recording is not None:
        recording = recording.resolve()
        error = recording_path_error(session, recording)
        if error is not None:
            raise SessionError(error)
    session.mkdir(mode=0o700, parents=True, exist_ok=True)
    with session_lease(session) as lease:
        refuse_abandoned_ownership(session)
        return _run_session(
            session,
            command,
            lease=lease,
            recording=recording,
            captions=captions,
            click_pings=click_pings,
            console=console,
            compact=compact,
            size=size,
            x11=x11,
            retain_input_content=retain_input_content,
            fonts=fonts,
        )


def _run_session(
    session: Path,
    command: list[str],
    *,
    lease: int,
    recording: Path | None,
    captions: CaptionMode,
    click_pings: bool,
    console: ConsolePanel | None,
    compact: bool,
    size: tuple[int, int],
    x11: bool,
    retain_input_content: bool,
    fonts: str,
) -> int:
    state = session / "session.json"
    if state.exists():
        raise SessionError(
            f"{state} already exists. Run framewisp SESSION recover after the owner exits."
        )

    (session / "app-exit.json").unlink(missing_ok=True)
    (session / "inputs.jsonl").write_text("", encoding="utf-8")
    (session / "activity.jsonl").write_text("", encoding="utf-8")
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
        with (
            headless_runtime(session, lease) as runtime,
            ExitStack() as stack,
        ):
            directory = str(runtime)
            env = session_environment_for_run(runtime, dict(os.environ))
            app_env = sanitized_environment(
                runtime,
                session_environment_for_run(runtime, caller_environment()),
                fonts=fonts,
            )
            for process_env in (env, app_env):
                process_env["FRAMEWISP_OWNER_FD"] = str(lease)
                process_env["FRAMEWISP_OWNER_RUNTIME"] = directory
            buses = start_inspection_buses(stack, runtime, session, env, app_env, stop)
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
            app_env["WAYLAND_DISPLAY"] = display
            x11_display = None
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
            console_capture = ConsoleCapture(session)
            output = stack.enter_context(app_output(console_capture))
            recordings = Recordings(session, env, stop, size, console_capture)
            stack.callback(recordings.close)
            if recording is not None:
                error = recordings.start(
                    recording,
                    captions=captions,
                    click_pings=click_pings,
                    console=console,
                    compact=compact,
                )
                if error is not None:
                    if stop.is_set():
                        return 0
                    raise SessionError(error)

            command = app_command(command, app_env)
            # Drain final app bytes before rendering the last recording.
            stack.callback(console_capture.wait)
            stack.callback(output.close)
            app = stack.enter_context(
                managed_process(
                    command, log=session / "app.log", env=app_env, output=output
                )
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
                size=size,
                retain_input_content=retain_input_content,
                capture=lambda path, cancelled: batch_screenshot(
                    recordings, path, cancelled
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
                            "console_panels": True,
                            "compact_recording": True,
                            "click_pings_recording": True,
                            "caption_modes": True,
                            "persistent_input": True,
                            "retain_input_content": retain_input_content,
                            "inspection_protocol": 1,
                            "accessibility_bus": env["AT_SPI_BUS_ADDRESS"],
                            "batch_input": True,
                            "runtime_directory": directory,
                            "wayland_display": display,
                            "width": size[0],
                            "height": size[1],
                            "x11_display": x11_display,
                            "recording": (
                                str(recordings.destination)
                                if recordings.destination is not None
                                else None
                            ),
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
                console_capture.check()
                result = app.poll()
                if result is not None:
                    temporary = session / ".app-exit.json"
                    temporary.write_text(
                        json.dumps({"returncode": result, "time": time.time()}) + "\n"
                    )
                    temporary.replace(session / "app-exit.json")
                    print(
                        app_exit_message(session, result), file=sys.stderr, flush=True
                    )
                    return result if result >= 0 else 128 - result
                monitored = backends.copy()
                if recordings.process is not None:
                    monitored["recorder"] = recordings.process
                for name, process in monitored.items():
                    if process.poll() is not None:
                        raise log_failure(f"{name} exited", session / (name + ".log"))
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
        if not (session / ".headless-owner.json").exists():
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
    session: Path,
    destination: Path,
    *,
    region: tuple[int, int, int, int] | None = None,
    metadata: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    if metadata and str(destination) == "-":
        raise SessionError(
            "screenshot --json requires a file path; '-' writes PNG to stdout."
        )
    if str(destination) != "-":
        # A runner from an older framewisp publishes no recording key.
        recording = json.loads((session / "session.json").read_text()).get("recording")
        error = capture_path_error(
            destination, Path(recording) if isinstance(recording, str) else None
        )
        if error is not None:
            raise SessionError(error)
    selector = ["-o", "HEADLESS-1"]
    width = height = 0
    x = y = 0
    display_width = display_height = 0
    if region is not None or metadata:
        state = json.loads((session / "session.json").read_text())
        display_width, display_height = state.get("width"), state.get("height")
        if (
            type(display_width) is not int
            or type(display_height) is not int
            or display_width <= 0
            or display_height <= 0
        ):
            raise SessionError(
                "Screenshot metadata requires display dimensions. "
                "Restart the session with this version of framewisp."
            )
        x, y, width, height = region or (0, 0, display_width, display_height)
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise SessionError(
                "--region requires nonnegative X and Y and positive WIDTH and HEIGHT."
            )
        if x + width > display_width or y + height > display_height:
            raise SessionError(
                f"--region {x} {y} {width} {height} exceeds the "
                f"{display_width}x{display_height} display."
            )
        if region is not None:
            selector = ["-g", f"{x},{y} {width}x{height}"]
    started = time.monotonic()
    result = display_command(
        session,
        ["grim", "-t", "png", *selector, str(destination)],
        env=session_environment(session),
        timeout=10,
        cancelled=cancelled,
    )
    if metadata and result == 0:
        print(
            json.dumps(
                {
                    "path": str(destination.resolve()),
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                    "display_width": display_width,
                    "display_height": display_height,
                    "capture_seconds": time.monotonic() - started,
                }
            )
        )
    return result


def batch_screenshot(
    recordings: Recordings, destination: Path, cancelled: Callable[[], bool]
) -> int:
    """Publish only a complete PNG, without overwriting another client's artifact."""
    try:
        with tempfile.TemporaryDirectory(
            prefix=".framewisp-capture-", dir=destination.parent
        ) as directory:
            temporary = Path(directory) / "capture.png"
            screenshot(recordings.session, temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError(
                    "Capture cancelled; caller disconnected or session stopped."
                )
            # A recording may have started since this batch was validated.
            error = capture_path_error(destination, recordings.destination)
            if error is not None:
                raise SessionError(error)
            destination.hardlink_to(temporary)
    except InterruptedError:
        raise
    except OSError as error:
        raise SessionError(f"Cannot save capture to {destination}: {error}") from None
    return 0

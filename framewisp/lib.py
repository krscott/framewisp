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
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Any, Literal, cast, get_args

from framewisp.actions import InputAction
from framewisp.batch import MAX_REQUEST_BYTES, Batch
from framewisp.compact import ffprobe
from framewisp.connection import (
    COMMAND_PROTOCOL,
    parse_request_fields,
    receive_request,
    reply,
    resolved_request_path,
    screenshot_path_error,
)
from framewisp.console import ConsoleCapture, app_output
from framewisp.environment import (
    caller_environment,
    configured_fonts,
    fixed_fonts,
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
from framewisp.finalization import (
    KEY_CAPTIONS,
    POINTER_CAPTIONS,
    CaptionOptions,
    capture_origin,
    render_clip,
)
from framewisp.input_log import CaptionCapture
from framewisp.inputs import VNC, InputWorker
from framewisp.keys import key_commands as key_commands
from framewisp.layout import ClipLayout, ConsolePanel, clip_layout
from framewisp.ownership import (
    ACTIVE_RECORDING,
    headless_runtime,
    kept_clip,
    refuse_abandoned_ownership,
    session_lease,
)
from framewisp.processes import OwnedProcess, managed_process, run_owned
from framewisp.recorder import recorder_output

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
        QT_QPA_PLATFORM="wayland",
        GSK_RENDERER="cairo",
    )
    # Lets Chromium connect to the session's accessibility bus on a headless
    # host. A caller's own value, such as 0, wins.
    env.setdefault("ACCESSIBILITY_ENABLED", "1")
    return env


def app_command(command: list[str], env: dict[str, str]) -> list[str]:
    """Make the bundled demo app available to nix run without changing the app PATH."""
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
    session_dir: Path,
    env: dict[str, str],
    app_env: dict[str, str],
    stop: Event,
) -> dict[str, OwnedProcess] | None:
    """Own both buses and the registry; never activate the desktop's services."""
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
        log = session_dir / (name + ".log")
        process = stack.enter_context(
            managed_process(
                [
                    # Resolved here because the bus runs with the app's
                    # environment, whose PATH need not contain dbus-daemon.
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
        managed_process([registry], log=session_dir / "registry.log", env=env)
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
        # rather than guessing a number or inheriting the desktop's DISPLAY.
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
    """Own one input connection. Connection loss stops access without replay.

    It lasts the whole session so the seat keeps its keyboard and pointer between
    commands (R-INPUT-1). Qt dismisses an open context menu when the last input
    client disconnects.
    """
    # Only the runner needs Twisted; loading it in every session command adds latency.
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
    captions: CaptionOptions,
    caption_capture: CaptionCapture,
    click_pings: bool,
    console: ConsoleCapture,
    compact: bool = False,
    layout: ClipLayout,
) -> Generator[OwnedProcess | None, None, None]:
    """Wait for the MP4 header before yielding; finalize while Sway is still alive.

    Everything up to publishing works on a private clip, so nothing but
    `publish_clip` can touch the destination.
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
        # The recorder tags its H.264 as full-range, but its default conversion
        # is limited-range, so white would decode as 235 instead of 255 (R-REC-4).
        "-F",
        "scale=out_range=full",
        "-m",
        "mp4",
        "-f",
        str(clip),
    ]
    timed = (
        captions.enabled or click_pings or layout.console_panel is not None or compact
    )
    capture_literals = captions.key == "all"
    # The recorder's protocol trace carries its first frame's timestamp, which is
    # the clip's time origin.
    recorder_env = env | {"WAYLAND_DEBUG": "client"} if timed else env
    capturing = False
    try:
        active.unlink(missing_ok=True)
        active.symlink_to(clip)
        with (
            caption_capture.clip(enabled=capture_literals) as literal_captions,
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
                stopped = (
                    caption_capture.stop() if capture_literals else time.monotonic()
                )
        if process.returncode != 0:
            raise log_failure(f"Recorder failed to finalize {destination}", log)
        failure = None
        try:
            # A clip with no duration has no video to render on (R-REC-22).
            if restore_hidden_frames(clip, log=log, env=env) and timed:
                render_clip(
                    clip,
                    input_log=log.parent / "inputs.jsonl",
                    origin=origin,
                    stopped=stopped,
                    log=log.parent / "finalization.log",
                    captions=captions,
                    literal_captions=literal_captions,
                    click_pings=click_pings,
                    console=console,
                    compact=compact,
                    layout=layout,
                    env=env,
                )
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            failure = error
        publish_clip(clip, destination, failure)
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
            tempfile.mkdtemp(prefix=".framewisp-clip-", dir=destination.parent)
        )
    except OSError as error:
        raise RuntimeError(f"Cannot record to {destination}: {error}") from None
    try:
        probe_destination(directory, destination)
    except RuntimeError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory / "clip.mp4"


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
                f"links, so a clip can never replace an existing file ({error}). "
                "Choose a path on another filesystem."
            ) from None
        link.unlink()
        shutil.rmtree(names)
    except OSError as error:
        raise RuntimeError(f"Cannot record to {destination}: {error}") from None


def publish_clip(clip: Path, destination: Path, failure: Exception | None) -> None:
    """Link a clip into place, which fails rather than replaces a file.

    Report `failure` too, naming wherever the clip ended up.
    """
    try:
        destination.hardlink_to(clip)
    except OSError as error:
        cause = f" {failure}" if failure is not None else ""
        raise RuntimeError(
            f"Cannot save clip to {destination}: {error}.{cause}"
        ) from None
    shutil.rmtree(clip.parent, ignore_errors=True)
    if failure is not None:
        raise RuntimeError(str(failure).replace(str(clip), str(destination)))


def new_file_error(
    session_dir: Path,
    path: Path,
    kind: Literal["clip destination", "screenshot path"],
) -> str | None:
    """Refuse a path for a new file that exists or that the session uses."""
    reserved = {
        (session_dir / name).resolve()
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
            "finalization.log",
            "dbus.log",
            "accessibility.log",
            "registry.log",
        )
    }
    if path in reserved:
        return f"{path} is reserved for session files. Choose a new {kind}."
    if path.exists():
        return f"{path} already exists. Choose a new {kind}."
    return None


@dataclass(frozen=True)
class ClipProbe:
    """What ffprobe measured. A clip can have no stream or container duration."""

    duration: float | None
    width: int | None
    height: int | None


def probe_clip(path: Path, *, env: dict[str, str]) -> ClipProbe:
    output = ffprobe(
        path,
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration:stream=width,height",
        "-of",
        "json",
        env=env,
    )
    data = cast(dict[str, Any], json.loads(output))
    duration = data["format"].get("duration")
    streams = cast(list[dict[str, Any]], data["streams"])
    stream = streams[0] if streams else {}
    return ClipProbe(
        duration=float(duration) if duration is not None else None,
        width=stream.get("width"),
        height=stream.get("height"),
    )


def restore_hidden_frames(clip: Path, *, log: Path, env: dict[str, str]) -> bool:
    """Remux a clip whose edit list hides its frames (R-REC-3).

    Return whether the clip has a duration. A failed remux is logged because the
    clip stays usable as evidence, and its summary reports the null (R-REC-22).
    """
    probe = probe_clip(clip, env=env)
    if probe.duration is not None:
        return True
    if probe.width is None:
        return False
    remuxed = clip.with_name("remuxed.mp4")
    remux_log = clip.with_name("remux.log")
    try:
        result = run_owned(
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
            log=remux_log,
            env=env,
            timeout=10,
        )
        problem = remux_log.read_text(errors="replace").strip() if result else None
    except (subprocess.TimeoutExpired, OSError) as error:
        problem = str(error)
    with suppress(OSError):
        remux_log.unlink(missing_ok=True)
    if problem is not None:
        remuxed.unlink(missing_ok=True)
        try:
            with log.open("a", errors="backslashreplace") as output:
                output.write(f"Could not restore frames in {clip}: {problem}\n")
        except OSError:
            pass  # The disk that failed the remux can also refuse this note.
        return False
    remuxed.replace(clip)
    return probe_clip(clip, env=env).duration is not None


def clip_summary(destination: Path, *, env: dict[str, str]) -> dict[str, object]:
    probe = probe_clip(destination, env=env)
    return {
        "path": str(destination),
        "duration_seconds": probe.duration,
        "width": probe.width,
        "height": probe.height,
        "size_bytes": destination.stat().st_size,
    }


@dataclass
class Recording:
    session_dir: Path
    env: dict[str, str]
    stop_requested: Event
    size: tuple[int, int]
    console_capture: ConsoleCapture
    caption_capture: CaptionCapture = field(default_factory=CaptionCapture)
    process: OwnedProcess | None = None
    resources: ExitStack = field(default_factory=ExitStack)
    destination: Path | None = None
    last_summary: dict[str, object] | None = None

    def start(
        self,
        destination: Path,
        *,
        captions: CaptionOptions = CaptionOptions(),
        click_pings: bool = True,
        console_panel: ConsolePanel | None = None,
        compact: bool = False,
    ) -> str | None:
        if self.process is not None:
            return "A recording is already active. Use record-stop first."
        if any(dimension % 2 for dimension in self.size):
            return "Recording requires even display width and height."
        layout = clip_layout(self.size, console_panel)
        if isinstance(layout, str):
            return layout
        try:
            error = new_file_error(self.session_dir, destination, "clip destination")
            if error is not None:
                return error
            self.process = self.resources.enter_context(
                start_recording(
                    destination,
                    log=self.session_dir / "recorder.log",
                    env=self.env,
                    stop=self.stop_requested,
                    captions=captions,
                    caption_capture=self.caption_capture,
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
                self.last_summary = clip_summary(self.destination, env=self.env)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            raise SessionError(self.with_kept_clip(str(error))) from None
        finally:
            self.process = None
            self.destination = None

    def with_kept_clip(self, error: str) -> str:
        clip = kept_clip(self.session_dir)
        return f"{error}\nThe clip remains at {clip}." if clip is not None else error


def session_command(
    session_dir: Path,
    command: str,
    clip_destination: Path | None = None,
    *,
    captions: CaptionOptions = CaptionOptions(),
    click_pings: bool = True,
    console_panel: ConsolePanel | None = None,
    compact: bool = False,
    parameters: dict[str, object] | None = None,
) -> int:
    state = json.loads((session_dir / "session.json").read_text())
    request: dict[str, object] = {
        "command": command,
        "session_directory": str(session_dir),
        "clip_destination": (
            str(clip_destination.resolve()) if clip_destination else None
        ),
        "click_pings": click_pings,
        "compact": compact,
        "console_panel": (
            console_panel.parameters() if console_panel is not None else None
        ),
        "parameters": parameters,
    }
    if command == "record-start":
        request.update(pointer_captions=captions.pointer, key_captions=captions.key)
    message = (json.dumps(request) + "\n").encode()
    if len(message) > MAX_REQUEST_BYTES:
        raise SessionError("Session request exceeds the 1 MiB limit.")
    endpoint = Path(state["command_socket"])
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(endpoint))
            connection.sendall(message)
            with connection.makefile("r") as response:
                line = response.readline()
    except OSError as connection_error:
        raise SessionError(
            f"Cannot reach session {session_dir} at {endpoint}: {connection_error}\n{SOCKET_ACCESS_HINT}"
        ) from None
    if not line:
        print(
            f"Session {session_dir} disconnected before {command} completed.",
            file=sys.stderr,
        )
        return 1
    result = json.loads(line)
    error = result["error"]
    if result["data"] is not None and (error is None or command == "batch"):
        print(json.dumps(result["data"]))
    if error is not None:
        print(error, file=sys.stderr)
        return 1
    return 0


@dataclass(frozen=True)
class RecordStart:
    clip_destination: Path
    captions: CaptionOptions
    click_pings: bool
    console_panel: ConsolePanel | None
    compact: bool

    @staticmethod
    def parse(fields: dict[str, object]) -> "RecordStart":
        console_panel = ConsolePanel.parse(fields.get("console_panel"))
        clip_destination = fields.get("clip_destination")
        pointer_captions = fields.get("pointer_captions")
        key_captions = fields.get("key_captions")
        click_pings = fields.get("click_pings", True)
        compact = fields.get("compact", False)
        if not isinstance(clip_destination, str):
            raise ValueError("record-start requires a clip_destination")
        if pointer_captions is not None and pointer_captions not in POINTER_CAPTIONS:
            raise ValueError(
                f"pointer_captions must be one of {', '.join(POINTER_CAPTIONS)}"
            )
        if key_captions is not None and key_captions not in KEY_CAPTIONS:
            raise ValueError(f"key_captions must be one of {', '.join(KEY_CAPTIONS)}")
        if not isinstance(click_pings, bool):
            raise ValueError("click_pings must be true or false")
        if not isinstance(compact, bool):
            raise ValueError("compact must be true or false")
        path = Path(clip_destination)
        if not path.is_absolute():
            raise ValueError("clip_destination must be absolute in session requests.")
        return RecordStart(
            resolved_request_path(path, "clip_destination"),
            CaptionOptions(
                pointer=pointer_captions,
                key=key_captions,
            ),
            click_pings,
            console_panel,
            compact,
        )


ParameterlessCommand = Literal["stop", "status", "record-stop"]
PARAMETERLESS_COMMANDS: tuple[ParameterlessCommand, ...] = get_args(
    ParameterlessCommand
)
SessionRequest = ParameterlessCommand | RecordStart | InputAction | Batch


def parse_batch(raw: object, recording: Recording) -> Batch:
    batch = Batch.parse(raw)
    for kind, screenshot in (
        ("screenshot", batch.screenshot),
        ("failure_screenshot", batch.failure_screenshot),
    ):
        if screenshot is None:
            continue
        if not screenshot.path.parent.is_dir():
            raise ValueError(f"{kind}.path requires an existing parent directory.")
        resolved = resolved_request_path(screenshot.path, f"{kind}.path")
        path_error = new_file_error(recording.session_dir, resolved, "screenshot path")
        if path_error is not None:
            raise ValueError(path_error)
        error = screenshot_path_error(resolved, recording.destination)
        if error is not None:
            raise ValueError(error)
    return batch


def parse_request(line: bytes, recording: Recording) -> SessionRequest:
    """Validate one request line, changing no session state."""
    fields = parse_request_fields(line)
    if fields.get("session_directory") != str(recording.session_dir):
        raise ValueError(
            "Session metadata points to a different session. Use the original session directory."
        )
    command = fields.get("command")
    if not isinstance(command, str):
        raise ValueError("command must be a string.")
    if command in PARAMETERLESS_COMMANDS:
        return command
    if command == "record-start":
        return RecordStart.parse(fields)
    if command == "batch":
        return parse_batch(fields.get("parameters"), recording)
    if command in {"move", "click", "drag", "scroll", "type", "key"}:
        return InputAction.parse(command, fields.get("parameters"))
    raise ValueError("Unsupported headless session command.")


def handle_session_command(
    listener: socket.socket,
    recording: Recording,
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
        request: SessionRequest | None = receive_request(
            connection,
            lambda failure: reply(connection, error=failure),
            lambda line: parse_request(line, recording),
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
                error = recording.finish()
            except SessionError as failure:
                # The session still fails; the caller needs the reason and raw path.
                reply(connection, error=str(failure))
                raise
            if error is None:
                data = recording.last_summary
        else:
            error = recording.start(
                request.clip_destination,
                captions=request.captions,
                click_pings=request.click_pings,
                console_panel=request.console_panel,
                compact=request.compact,
            )
        write_state()
        reply(connection, error=error, data=data)
    finally:
        if not stopping:
            connection.close()
    return None


def run_session(
    session_dir: Path,
    command: list[str],
    *,
    clip_destination: Path | None = None,
    captions: CaptionOptions = CaptionOptions(),
    click_pings: bool = True,
    console_panel: ConsolePanel | None = None,
    compact: bool = False,
    size: tuple[int, int] = (1280, 720),
    x11: bool = False,
    real_env: bool = False,
) -> int:
    fonts = configured_fonts(os.environ)
    if fonts is None:
        raise SessionError(
            "run requires FRAMEWISP_FONTCONFIG_FILE, a Fontconfig file that selects "
            "a fixed set of fonts. Use the Nix package or nix develop, or set it to "
            "your own file. Pointing it at the host's fonts.conf works, but output "
            "can then differ between hosts."
        )
    session_dir = session_dir.resolve()
    if clip_destination is not None:
        clip_destination = clip_destination.resolve()
        error = new_file_error(session_dir, clip_destination, "clip destination")
        if error is not None:
            raise SessionError(error)
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with session_lease(session_dir) as lease:
        refuse_abandoned_ownership(session_dir)
        return _run_session(
            session_dir,
            command,
            lease=lease,
            clip_destination=clip_destination,
            captions=captions,
            click_pings=click_pings,
            console_panel=console_panel,
            compact=compact,
            size=size,
            x11=x11,
            fonts=fonts,
            real_env=real_env,
        )


def _run_session(
    session_dir: Path,
    command: list[str],
    *,
    lease: int,
    clip_destination: Path | None,
    captions: CaptionOptions,
    click_pings: bool,
    console_panel: ConsolePanel | None,
    compact: bool,
    size: tuple[int, int],
    x11: bool,
    fonts: str,
    real_env: bool,
) -> int:
    state = session_dir / "session.json"
    if state.exists():
        raise SessionError(
            f"{state} already exists. Run framewisp SESSION recover after the owner exits."
        )

    (session_dir / "app-exit.json").unlink(missing_ok=True)
    (session_dir / "inputs.jsonl").write_text("", encoding="utf-8")
    (session_dir / "activity.jsonl").write_text("", encoding="utf-8")
    stop = Event()
    stop_connection: socket.socket | None = None
    shutdown_error: str | None = None
    recording: Recording | None = None

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    previous_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with (
            headless_runtime(session_dir, lease) as runtime,
            ExitStack() as stack,
        ):
            directory = str(runtime)
            # Compositor title bars must not use the caller's fonts either.
            env = fixed_fonts(
                session_environment_for_run(runtime, dict(os.environ)), fonts=fonts
            )
            # An empty path selects the compositor's built-in cursor, not the caller's.
            env["XCURSOR_PATH"] = ""
            app_env = caller_environment()
            if not real_env:
                app_env = sanitized_environment(runtime, app_env, fonts=fonts)
            app_env = session_environment_for_run(runtime, app_env)
            for process_env in (env, app_env):
                process_env["FRAMEWISP_OWNER_FD"] = str(lease)
                process_env["FRAMEWISP_OWNER_RUNTIME"] = directory
            buses = start_inspection_buses(
                stack, runtime, session_dir, env, app_env, stop
            )
            if buses is None:
                return 0
            compositor = stack.enter_context(
                start_sway(
                    runtime,
                    log=session_dir / "sway.log",
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
                    runtime, process=sway, log=session_dir / "sway.log", stop=stop
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
                start_wayvnc(
                    runtime, log=session_dir / "wayvnc.log", env=env, stop=stop
                )
            )
            if vnc is None:
                return 0
            input_client = stack.enter_context(keep_input_devices(runtime, stop))

            backends = buses | {"sway": sway, "wayvnc": vnc}
            console_capture = ConsoleCapture(session_dir)
            output = stack.enter_context(app_output(console_capture))
            recording = Recording(session_dir, env, stop, size, console_capture)
            stack.callback(recording.close)
            if clip_destination is not None:
                error = recording.start(
                    clip_destination,
                    captions=captions,
                    click_pings=click_pings,
                    console_panel=console_panel,
                    compact=compact,
                )
                if error is not None:
                    if stop.is_set():
                        return 0
                    raise SessionError(error)

            command = app_command(command, app_env)
            # Drain final app bytes before finalizing the last clip.
            stack.callback(console_capture.wait)
            stack.callback(output.close)
            app = stack.enter_context(
                managed_process(
                    command, log=session_dir / "app.log", env=app_env, output=output
                )
            )
            command_socket = str(runtime / "command.sock")
            listener = stack.enter_context(socket.socket(socket.AF_UNIX))
            listener.bind(command_socket)
            listener.listen()
            listener.settimeout(0.1)
            inputs = InputWorker(
                session_dir,
                input_client,
                stop,
                x11=x11,
                size=size,
                caption_capture=recording.caption_capture,
                screenshot=lambda path, cancelled: batch_screenshot(
                    recording, path, cancelled
                ),
            )
            stack.callback(inputs.close)

            def write_state() -> None:
                assert recording is not None
                processes = backends | {"app": app}
                if recording.process is not None:
                    processes["recorder"] = recording.process
                temporary = session_dir / ".session.json"
                temporary.write_text(
                    json.dumps(
                        {
                            "kind": "headless",
                            "command_protocol": COMMAND_PROTOCOL,
                            "accessibility_bus": env["AT_SPI_BUS_ADDRESS"],
                            "command_socket": command_socket,
                            "runtime_directory": directory,
                            "wayland_display": display,
                            "width": size[0],
                            "height": size[1],
                            "x11_display": x11_display,
                            "clip_destination": (
                                str(recording.destination)
                                if recording.destination is not None
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
                assert recording is not None
                return {
                    "session_directory": str(session_dir),
                    "status": "running",
                    "backend": "x11" if x11 else "wayland",
                    "wayland_display": display,
                    "x11_display": x11_display,
                    "width": size[0],
                    "height": size[1],
                    "app": {"pid": app.pid, "running": app.poll() is None},
                    "recording": (
                        {
                            "clip_destination": str(recording.destination),
                            "pid": recording.process.pid,
                            "running": recording.process.poll() is None,
                        }
                        if recording.process is not None
                        else None
                    ),
                }

            write_state()
            print(f"Session ready: {session_dir}", flush=True)
            while not stop.is_set():
                console_capture.check()
                result = app.poll()
                if result is not None:
                    temporary = session_dir / ".app-exit.json"
                    temporary.write_text(
                        json.dumps({"returncode": result, "time": time.time()}) + "\n"
                    )
                    temporary.replace(session_dir / "app-exit.json")
                    print(
                        app_exit_message(session_dir, result),
                        file=sys.stderr,
                        flush=True,
                    )
                    return result if result >= 0 else 128 - result
                monitored = backends.copy()
                if recording.process is not None:
                    monitored["recorder"] = recording.process
                for name, process in monitored.items():
                    if process.poll() is not None:
                        raise log_failure(
                            f"{name} exited", session_dir / (name + ".log")
                        )
                stop_connection = handle_session_command(
                    listener, recording, write_state, status, inputs
                )
                if stop_connection is not None:
                    stop.set()
            return 0
    except BaseException as error:
        # A stop caller must not receive success when shutdown or encoding failed.
        shutdown_error = str(error)
        raise
    finally:
        if not (session_dir / ".headless-owner.json").exists():
            state.unlink(missing_ok=True)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if stop_connection is not None:
            with stop_connection:
                reply(
                    stop_connection,
                    error=shutdown_error,
                    data={
                        "session_directory": str(session_dir),
                        "status": "stopped",
                        "clip": recording.last_summary if recording else None,
                    },
                )


def session_environment(session_dir: Path) -> dict[str, str]:
    state = json.loads((session_dir / "session.json").read_text())
    return os.environ | {
        "XDG_RUNTIME_DIR": state["runtime_directory"],
        "WAYLAND_DISPLAY": state["wayland_display"],
    }


def screenshot(
    session_dir: Path,
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
        # Read the active clip's destination from session metadata, not from the
        # runner: record-stop renders in the runner's command loop, so asking
        # the runner would wait for it.
        clip_destination = json.loads((session_dir / "session.json").read_text())[
            "clip_destination"
        ]
        error = screenshot_path_error(
            destination,
            Path(clip_destination) if clip_destination is not None else None,
        )
        if error is not None:
            raise SessionError(error)
    selector = ["-o", "HEADLESS-1"]
    width = height = 0
    x = y = 0
    display_width = display_height = 0
    if region is not None or metadata:
        state = json.loads((session_dir / "session.json").read_text())
        display_width, display_height = state["width"], state["height"]
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
    # grim, not the VNC connection: framebuffer requests over VNC timed out
    # while input worked, for an unknown reason. That was seen with Sway 1.12,
    # wlroots 0.20.2, wayvnc 0.10.1 and vncdotool 1.2.0.
    result = display_command(
        session_dir,
        ["grim", "-t", "png", *selector, str(destination)],
        env=session_environment(session_dir),
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
                    "screenshot_seconds": time.monotonic() - started,
                }
            )
        )
    return result


def batch_screenshot(
    recording: Recording, destination: Path, cancelled: Callable[[], bool]
) -> int:
    """Publish only a complete PNG, without overwriting another client's artifact."""
    try:
        with tempfile.TemporaryDirectory(
            prefix=".framewisp-screenshot-", dir=destination.parent
        ) as directory:
            temporary = Path(directory) / "screenshot.png"
            screenshot(recording.session_dir, temporary, cancelled=cancelled)
            if cancelled():
                raise InterruptedError(
                    "Screenshot cancelled; caller disconnected or session stopped."
                )
            # A recording may have started since this batch was validated.
            error = screenshot_path_error(destination, recording.destination)
            if error is not None:
                raise SessionError(error)
            destination.hardlink_to(temporary)
    except InterruptedError:
        raise
    except OSError as error:
        raise SessionError(
            f"Cannot save screenshot to {destination}: {error}"
        ) from None
    return 0

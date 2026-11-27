"""Command socket messages; no desktop libraries required."""

import json
import shlex
import socket
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar, cast, get_args

# The version of what a session command and a runner exchange: session metadata,
# requests and replies. A session command refuses a session that publishes
# another value, so bump it in any change to their fields or meaning.
COMMAND_PROTOCOL = 7
MAX_REQUEST_BYTES = 1024 * 1024
# A connection carries one request, and a client gets this long to send it.
REQUEST_SECONDS = 2.0
# What validators raise for a bad request. Anything else is a validator bug.
REJECTED_REQUEST = (ValueError, TypeError, OverflowError, OSError)

Request = TypeVar("Request")

# The `kind` that session metadata publishes.
SessionKind = Literal["headless", "attached", "teaching"]
# The kinds of session on the user's desktop, reached by `request_desktop`.
DesktopKind = Literal["attached", "teaching"]
DESKTOP_KINDS: frozenset[SessionKind] = frozenset(get_args(DesktopKind))
# The session commands a teaching session accepts; it only watches.
TEACHING_COMMANDS = frozenset({"screenshot", "teach-note"})


def session_kind(metadata: object) -> SessionKind | None:
    """Return the kind that parsed session metadata publishes.

    None means it publishes no known kind. Such a session is not a desktop
    session, and session commands refuse it.
    """
    if not isinstance(metadata, dict):
        return None
    kind = cast(dict[str, object], metadata).get("kind")
    # Compare by equality: an unhashable kind cannot be looked up in a set.
    return next((known for known in get_args(SessionKind) if known == kind), None)


def teaching_refusal(command: str) -> str:
    return (
        f"{command} is refused: this is a teaching session, which only watches. "
        "It accepts only screenshot and teach-note. "
        "The user ends this session with Ctrl+C in its terminal."
    )


def desktop_session(kind: DesktopKind) -> str:
    return "teaching session" if kind == "teaching" else "attached session"


def restart_remedy(kind: DesktopKind) -> str:
    """Say how the user starts a desktop session of this kind again."""
    if kind == "teaching":
        return "Ask the user to run teach again with a new lesson directory."
    return "Ask the user to run attach in another terminal."


def disconnected(kind: DesktopKind) -> str:
    return (
        f"{desktop_session(kind).capitalize()} is disconnected. {restart_remedy(kind)}"
    )


@dataclass(frozen=True)
class HeadlessSessionMetadata:
    session_directory: Path
    command_socket: Path
    runtime_directory: Path
    accessibility_bus: str
    wayland_display: str
    width: int
    height: int
    x11_display: str | None = None
    clip_destination: Path | None = None


@dataclass(frozen=True)
class DesktopSessionMetadata:
    session_directory: Path
    kind: DesktopKind
    command_socket: Path
    share_token: str


SessionMetadata = HeadlessSessionMetadata | DesktopSessionMetadata


class _MetadataError(Exception):
    pass


def _field(data: dict[str, object], name: str, path: Path) -> object:
    if name not in data:
        raise _MetadataError(
            f"Cannot read session metadata: {path} is missing {name!r}."
        )
    return data[name]


def _string(data: dict[str, object], name: str, path: Path) -> str:
    value = _field(data, name, path)
    if not isinstance(value, str) or not value or "\0" in value:
        raise _MetadataError(
            f"Cannot read session metadata: {path} has invalid {name!r} "
            "(expected a nonempty string)."
        )
    return value


def _optional_string(data: dict[str, object], name: str, path: Path) -> str | None:
    value = _field(data, name, path)
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\0" in value:
        raise _MetadataError(
            f"Cannot read session metadata: {path} has invalid {name!r} "
            "(expected a nonempty string or null)."
        )
    return value


def _path(data: dict[str, object], name: str, path: Path) -> Path:
    value = _field(data, name, path)
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or not Path(value).is_absolute()
    ):
        raise _MetadataError(
            f"Cannot read session metadata: {path} has invalid {name!r} "
            "(expected an absolute path)."
        )
    return Path(value)


def _optional_path(data: dict[str, object], name: str, path: Path) -> Path | None:
    value = _field(data, name, path)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or not Path(value).is_absolute()
    ):
        raise _MetadataError(
            f"Cannot read session metadata: {path} has invalid {name!r} "
            "(expected an absolute path or null)."
        )
    return Path(value)


def _positive_int(data: dict[str, object], name: str, path: Path) -> int:
    value = _field(data, name, path)
    if type(value) is not int or value <= 0:
        raise _MetadataError(
            f"Cannot read session metadata: {path} has invalid {name!r} "
            "(expected a positive integer)."
        )
    return value


def parse_session_metadata(raw: object, session_dir: Path) -> SessionMetadata | str:
    """Validate protocol version and fields, returning typed metadata or an error message."""
    metadata_path = session_dir / "session.json"
    if not isinstance(raw, dict):
        return f"Cannot read session metadata: {metadata_path} must be a JSON object."
    data = cast(dict[str, object], raw)
    protocol = data.get("command_protocol")
    published_kind = session_kind(data)
    # Every session command parses here, so no caller can hold metadata of
    # another command protocol version (R-CONTROL-3).
    if type(protocol) is not int or protocol != COMMAND_PROTOCOL:
        published = (
            "no command protocol"
            if protocol is None
            else f"command protocol {protocol!r}"
        )
        if published_kind in DESKTOP_KINDS:
            remedy = (
                "Ask the user to end it with Ctrl+C in its terminal if it is "
                "still running, then to start it again with this version "
                "(for teach, with a new lesson directory)."
            )
        else:
            remedy = (
                "Stop its runner with Ctrl+C or SIGTERM. If the runner has "
                f"died, run `{shlex.join(['framewisp', str(session_dir), 'recover'])}` "
                "with the framewisp version that started the session. Then start the "
                "session again with this version."
            )
        return (
            f"Session {session_dir} was started by another version of framewisp: "
            f"it publishes {published} and this version uses {COMMAND_PROTOCOL}. "
            f"{remedy}"
        )
    if published_kind is None:
        return (
            f"Cannot read session metadata: {metadata_path} publishes no known "
            "session kind."
        )
    try:
        if published_kind == "headless":
            return HeadlessSessionMetadata(
                session_directory=session_dir,
                command_socket=_path(data, "command_socket", metadata_path),
                runtime_directory=_path(data, "runtime_directory", metadata_path),
                accessibility_bus=_string(data, "accessibility_bus", metadata_path),
                wayland_display=_string(data, "wayland_display", metadata_path),
                width=_positive_int(data, "width", metadata_path),
                height=_positive_int(data, "height", metadata_path),
                x11_display=_optional_string(data, "x11_display", metadata_path),
                clip_destination=_optional_path(
                    data, "clip_destination", metadata_path
                ),
            )
        return DesktopSessionMetadata(
            session_directory=session_dir,
            kind=published_kind,
            command_socket=_path(data, "command_socket", metadata_path),
            share_token=_string(data, "share_token", metadata_path),
        )
    except _MetadataError as error:
        return str(error)


def read_session_metadata(session_dir: Path) -> SessionMetadata | str:
    """Read, version-check and parse `session.json` once."""
    metadata_path = session_dir / "session.json"
    try:
        raw: object = json.loads(metadata_path.read_text())
    except FileNotFoundError:
        return (
            "Session is disconnected. Start run, or ask the user to run attach "
            "or teach in another terminal."
        )
    except (OSError, ValueError, RecursionError) as error:
        return f"Cannot read session metadata: {error}"
    return parse_session_metadata(raw, session_dir)


def request_desktop(
    session: DesktopSessionMetadata,
    command: str,
    parameters: dict[str, object],
) -> int:
    """Send a command to an attached or teaching session."""
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(session.command_socket))
            connection.sendall(
                (
                    json.dumps(
                        {
                            "command": command,
                            "parameters": parameters,
                            "share_token": session.share_token,
                        }
                    )
                    + "\n"
                ).encode()
            )
            with connection.makefile("r") as response:
                line = response.readline()
        if not line:
            print(disconnected(session.kind), file=sys.stderr)
            return 1
        result = json.loads(line)
        if result["error"]:
            print(result["error"], file=sys.stderr)
        return int(result["status"])
    except (FileNotFoundError, ConnectionError):
        print(disconnected(session.kind), file=sys.stderr)
        return 1

    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(
            f"Cannot use {desktop_session(session.kind)}: {error}. {restart_remedy(session.kind)}",
            file=sys.stderr,
        )
        return 1


def reply(
    connection: socket.socket,
    *,
    error: str | None = None,
    data: dict[str, object] | None = None,
) -> None:
    try:
        connection.sendall((json.dumps({"error": error, "data": data}) + "\n").encode())
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        # A caller can stop waiting while an action or recording finishes.
        pass


def read_line(connection: socket.socket) -> bytes:
    """Read one request line, however slowly the client sends it."""
    deadline = time.monotonic() + REQUEST_SECONDS
    data = bytearray()
    try:
        while b"\n" not in data and len(data) <= MAX_REQUEST_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(remaining)
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    except TimeoutError:
        raise ValueError("Timed out waiting for a complete session request.") from None
    finally:
        connection.settimeout(REQUEST_SECONDS)
    end = data.find(b"\n") + 1 or len(data)
    if end > MAX_REQUEST_BYTES:
        raise ValueError("Session request exceeds the 1 MiB limit.")
    return bytes(data[:end])


def parse_request_fields(line: bytes) -> dict[str, object]:
    try:
        raw: object = json.loads(line)
    except (ValueError, RecursionError) as failure:
        raise ValueError(f"Session request is not valid JSON: {failure}") from None
    if not isinstance(raw, dict):
        raise ValueError("Expected a session request object.")
    return cast(dict[str, object], raw)


def resolved_request_path(path: Path, field: str) -> Path:
    """Resolve a path from a request, refusing what the filesystem API cannot encode."""
    try:
        return path.resolve()
    except (ValueError, RuntimeError) as failure:
        # NUL and most lone surrogates raise ValueError. Before Python 3.13, so
        # does a symlink loop, as RuntimeError.
        raise ValueError(f"{field} is not a usable path: {failure}") from None


def screenshot_path_error(
    destination: Path, clip_destination: Path | None
) -> str | None:
    """Refuse the active recording's clip destination, a file only once published.

    The check is not atomic with the write: a file that lands first stops the
    clip taking its destination (R-REC-18).
    """
    try:
        path = resolved_request_path(destination, "path")
    except ValueError as error:
        return str(error)
    if path == clip_destination:
        return (
            f"{path} is the clip destination of the active recording. "
            "Choose a new path."
        )
    return None


def receive_request(
    connection: socket.socket,
    reject: Callable[[str], None],
    parse: Callable[[bytes], Request],
) -> Request | None:
    """Read and validate a request, or reject it and return None.

    `parse` changes no session state, so every failure here leaves the session
    running. Execution happens outside, where an unexpected error stays a failure.
    """
    try:
        return parse(read_line(connection))
    except REJECTED_REQUEST as failure:
        reject(str(failure))
    except Exception as failure:
        name = type(failure).__name__
        reject(
            f"Framewisp bug: validating this request raised {name}. "
            "See the runner's stderr."
        )
        try:
            # The message is left out because it can quote typed text.
            print(f"Bug validating a session request: {name}", file=sys.stderr)
            traceback.print_tb(failure.__traceback__)
        except (OSError, ValueError):
            # A closed stderr must not turn a rejected request into a failure.
            pass
    return None

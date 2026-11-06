"""Session socket messages; no desktop libraries required."""

import json
import socket
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

from framewisp.batch import MAX_REQUEST_BYTES

# A connection carries one request, and a client gets this long to send it.
REQUEST_SECONDS = 2.0
# What validators raise for a bad request. Anything else is a validator bug.
REJECTED_REQUEST = (ValueError, TypeError, OverflowError, OSError)

Request = TypeVar("Request")


def request_attached(session: Path, action: str, parameters: dict[str, object]) -> int:
    try:
        state = json.loads((session / "session.json").read_text())
        if state.get("kind") != "attached":
            print("This command requires an attached desktop session.", file=sys.stderr)
            return 1
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(Path(state["runtime_directory"]) / "control.sock"))
            connection.sendall(
                (
                    json.dumps(
                        {
                            "action": action,
                            "parameters": parameters,
                            "attachment": state["attachment"],
                        }
                    )
                    + "\n"
                ).encode()
            )
            with connection.makefile("r") as response:
                line = response.readline()
        if not line:
            print("Attached session disconnected.", file=sys.stderr)
            return 1
        result = json.loads(line)
        if result["error"]:
            print(result["error"], file=sys.stderr)
        return int(result["status"])
    except (FileNotFoundError, ConnectionError):
        print(
            "Attached session is disconnected. Ask the user to run attach in another terminal.",
            file=sys.stderr,
        )
        return 1

    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(
            f"Cannot use attached session: {error}. Ask the user to reconnect.",
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


def parse_command(line: bytes) -> dict[str, object]:
    try:
        raw: object = json.loads(line)
    except (ValueError, RecursionError) as failure:
        raise ValueError(f"Session request is not valid JSON: {failure}") from None
    if not isinstance(raw, dict):
        raise ValueError("Expected a session command object.")
    return cast(dict[str, object], raw)


def resolved_request_path(path: Path, field: str) -> Path:
    """Resolve a requested path, refusing what the filesystem API cannot encode."""
    try:
        return path.resolve()
    except (ValueError, RuntimeError) as failure:
        # NUL and most lone surrogates raise ValueError. Before Python 3.13, so
        # does a symlink loop, as RuntimeError.
        raise ValueError(f"{field} is not a usable path: {failure}") from None


def capture_path_error(destination: Path, recording: Path | None) -> str | None:
    """Refuse the active recording's destination, which is no file until published."""
    try:
        path = resolved_request_path(destination, "path")
    except ValueError as error:
        return str(error)
    if path == recording:
        return f"{path} is the active recording's destination. Choose a new path."
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
            "See the owner's stderr."
        )
        try:
            # The message is left out because it can quote typed text.
            print(f"Bug validating a control request: {name}", file=sys.stderr)
            traceback.print_tb(failure.__traceback__)
        except (OSError, ValueError):
            # A closed stderr must not turn a rejected request into a failure.
            pass
    return None

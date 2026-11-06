"""How the runner tells a bad control request from a runner bug."""

import errno
import json
import socket
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from framewisp import connection
from framewisp.actions import InputAction
from framewisp.console import ConsoleCapture
from framewisp.errors import SessionError, display_command
from framewisp.inputs import InputWorker
from framewisp.lib import (
    Recordings,
    handle_session_command,
    parse_batch,
    recording_summary,
)

SECRET = "typed secret"


@pytest.fixture
def recordings(tmp_path: Path) -> Recordings:
    return Recordings(tmp_path, {}, Event(), (1280, 720), ConsoleCapture(tmp_path))


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[socket.socket]:
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(tmp_path / "control.sock"))
        server.listen()
        yield server


def handle_bytes(
    listener: socket.socket,
    recordings: Recordings,
    message: bytes,
    status: Callable[[], dict[str, object]] = dict,
) -> dict[str, object]:
    """Run one request to completion and return the reply."""
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(cast(str, listener.getsockname()))
        client.sendall(message)
        handle_session_command(
            listener,
            recordings,
            lambda: None,
            status,
            # A request rejected or failed before queueing never reaches the worker.
            cast(InputWorker, None),
        )
        return cast(dict[str, object], json.loads(client.makefile().readline()))


def handle(
    listener: socket.socket,
    recordings: Recordings,
    request: dict[str, object],
    status: Callable[[], dict[str, object]] = dict,
) -> dict[str, object]:
    message = {"session": str(recordings.session)} | request
    encoded = (json.dumps(message) + "\n").encode()
    return handle_bytes(listener, recordings, encoded, status)


def start_request(tmp_path: Path) -> dict[str, object]:
    return {
        "action": "record-start",
        "destination": str(tmp_path / "clip.mp4"),
        "captions": "keys",
    }


# R-CONTROL-1, R-CONTROL-2
def test_validator_bug_is_reported_without_ending_the_session(
    listener: socket.socket,
    recordings: Recordings,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def parse(action: object, raw: object) -> InputAction:
        raise KeyError(SECRET)

    monkeypatch.setattr(InputAction, "parse", parse)
    response = handle(
        listener,
        recordings,
        {"action": "type", "parameters": {"text": SECRET, "interval": 0}},
    )
    error = cast(str, response["error"])
    assert "Framewisp bug" in error and "KeyError" in error
    assert SECRET not in error
    stderr = capfd.readouterr().err
    assert "KeyError" in stderr and "in parse" in stderr
    assert SECRET not in stderr


class BrokenStderr:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def write(self, text: str) -> int:
        raise self.failure


# R-CONTROL-1, R-CONTROL-2
@pytest.mark.parametrize(
    "failure",
    [BrokenPipeError("stderr closed"), ValueError("I/O operation on closed file.")],
)
def test_validator_bug_with_a_closed_stderr_keeps_the_session(
    listener: socket.socket,
    recordings: Recordings,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    def parse(action: object, raw: object) -> InputAction:
        raise KeyError(SECRET)

    monkeypatch.setattr(InputAction, "parse", parse)
    monkeypatch.setattr(sys, "stderr", BrokenStderr(failure))
    response = handle(
        listener,
        recordings,
        {"action": "type", "parameters": {"text": SECRET, "interval": 0}},
    )
    assert "Framewisp bug" in cast(str, response["error"])


@pytest.mark.parametrize("failure", [ValueError, TypeError, OSError, KeyError])
def test_error_while_recording_starts_is_a_runner_failure(
    listener: socket.socket,
    recordings: Recordings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    def start(*args: object, **kwargs: object) -> str | None:
        raise failure("runner bug")

    monkeypatch.setattr(Recordings, "start", start)
    with pytest.raises(failure, match="runner bug"):
        handle(listener, recordings, start_request(tmp_path))


def test_error_while_reporting_status_is_a_runner_failure(
    listener: socket.socket, recordings: Recordings
) -> None:
    def status() -> dict[str, object]:
        raise ValueError("runner bug")

    with pytest.raises(ValueError, match="runner bug"):
        handle(listener, recordings, {"action": "status"}, status)


# R-CONTROL-1
def test_deadline_also_stops_a_client_that_keeps_sending(
    listener: socket.socket,
    recordings: Recordings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connection, "REQUEST_SECONDS", 0)
    response = handle_bytes(listener, recordings, b"{}\n")
    assert "Timed out waiting" in cast(str, response["error"])


@pytest.mark.parametrize("kind", ["capture", "failure_capture"])
def test_capture_through_a_symlink_cannot_be_the_active_recording(
    recordings: Recordings, tmp_path: Path, kind: str
) -> None:
    recordings.destination = tmp_path / "clip.mp4"
    (tmp_path / "alias").symlink_to(tmp_path)
    action = {"action": "move", "x": 0, "y": 0}
    capture = {"path": str(tmp_path / "alias" / "clip.mp4")}
    with pytest.raises(ValueError, match="active recording"):
        parse_batch({"actions": [action], kind: capture}, recordings)


# R-CONTROL-1
def test_stalled_client_gets_an_error_reply(
    listener: socket.socket, recordings: Recordings
) -> None:
    response = handle_bytes(listener, recordings, b'{"action"')
    assert "Timed out waiting" in cast(str, response["error"])


# R-CONTROL-1, R-CONTROL-2
def test_destination_lookup_failure_is_an_error_reply(
    listener: socket.socket,
    recordings: Recordings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 3.14 hides this errno, but earlier `Path.exists` raises it."""

    def exists(self: Path, *, follow_symlinks: bool = True) -> bool:
        raise OSError(errno.ENAMETOOLONG, "File name too long", str(self))

    monkeypatch.setattr(Path, "exists", exists)
    response = handle(listener, recordings, start_request(tmp_path))
    assert "File name too long" in cast(str, response["error"])


def test_tool_error_naming_a_non_utf8_path_stays_a_session_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "session.json").write_text(
        json.dumps({"wayland_display": "wayland-1", "runtime_directory": "/run"})
    )
    with pytest.raises(SessionError, match="caf"):
        display_command(
            tmp_path, ["sh", "-c", r"printf 'caf\351' >&2; exit 1"], timeout=5
        )


def test_inspecting_a_missing_non_utf8_recording_is_a_session_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(SessionError, match="Could not inspect recording"):
        recording_summary(tmp_path / "\udc80gone.mp4")

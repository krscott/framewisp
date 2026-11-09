"""Cancellation tests replace portal I/O; live consent is tested on the desktop."""

import errno
import json
import os
import pty
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event, Timer
from typing import cast
from unittest.mock import Mock

import pytest

from framewisp import attach
from framewisp.attach import (
    KEYCODES,
    AttachedInput,
    attach_session,
    open_destination,
    run_attachment,
    write_png,
)
from framewisp.connection import request_attached
from framewisp.desktop import detach_desktop, reserve_desktop
from framewisp.errors import SessionError
from framewisp.ownership import (
    recover_session,
    runtime_path,
    session_lease,
    write_journal,
)
from framewisp.portal import DesktopPortal
from framewisp.release_guard import RELEASE


@pytest.fixture
def portal_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, tuple[object, ...]]]:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def send(self: DesktopPortal, method: str, signature: str, *values: object) -> None:
        calls.append((method, values))

    monkeypatch.setattr(DesktopPortal, "input", send)
    return calls


def input_without_bus(stop: Event) -> AttachedInput:
    portal = object.__new__(DesktopPortal)
    portal.size = (800, 600)
    portal.node = 1
    return AttachedInput(portal, stop)


@pytest.mark.parametrize(
    "chord,code,modifiers",
    [
        ("Ctrl+comma", 51, (29,)),
        ("Ctrl+,", 51, (29,)),
        ("Ctrl+slash", 53, (29,)),
        ("Ctrl+backslash", 43, (29,)),
        ("Ctrl+Shift+comma", 51, (29, 42)),
        ("Ctrl+less", 51, (29, 42)),
        ("Ctrl++", 13, (29, 42)),
        ("Shift+plus", 13, (42,)),
        ("Home", 102, ()),
        ("End", 107, ()),
        ("Page_Up", 104, ()),
        ("Page_Down", 109, ()),
        ("Shift+Tab", 15, (42,)),
    ]
    + [(f"F{number}", 58 + number, ()) for number in range(1, 11)]
    + [("F11", 87, ()), ("F12", 88, ())],
)
def test_attached_key_codes_and_release(
    portal_calls: list[tuple[str, tuple[object, ...]]],
    chord: str,
    code: int,
    modifiers: tuple[int, ...],
) -> None:
    inputs = input_without_bus(Event())
    inputs.perform("key", {"chord": chord})
    expected = [
        *((modifier, 1) for modifier in modifiers),
        (code, 1),
        (code, 0),
        *((modifier, 0) for modifier in reversed(modifiers)),
    ]
    assert portal_calls == [("NotifyKeyboardKeycode", values) for values in expected]
    assert not inputs.keys


def test_invalid_attached_chord_sends_no_keys(
    portal_calls: list[tuple[str, tuple[object, ...]]],
) -> None:
    with pytest.raises(ValueError, match="Unsupported key combination"):
        input_without_bus(Event()).perform("key", {"chord": "Ctrl+unknown"})
    assert portal_calls == []


# R-INPUT-5
def test_stop_releases_drag_and_modifiers(
    portal_calls: list[tuple[str, tuple[object, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = Event()
    inputs = input_without_bus(stop)
    original_move = inputs.move

    def move_and_stop(x: int, y: int) -> None:
        original_move(x, y)
        if inputs.buttons:
            stop.set()

    monkeypatch.setattr(inputs, "move", move_and_stop)
    with pytest.raises(InterruptedError, match="disconnected"):
        inputs.perform(
            "drag",
            {
                "x1": 10,
                "y1": 10,
                "x2": 100,
                "y2": 100,
                "button": "left",
                "modifier": ["ctrl", "shift"],
                "duration": 0,
            },
        )
    assert portal_calls[-3:] == [
        ("NotifyPointerButton", (272, 0)),
        ("NotifyKeyboardKeycode", (KEYCODES["shift"], 0)),
        ("NotifyKeyboardKeycode", (KEYCODES["ctrl"], 0)),
    ]
    assert not inputs.keys and not inputs.buttons
    before = list(portal_calls)
    with pytest.raises(InterruptedError):
        inputs.perform("type", {"text": "never sent", "interval": 0})
    assert portal_calls == before


def test_invalid_drag_does_not_press_button(
    portal_calls: list[tuple[str, tuple[object, ...]]],
) -> None:
    inputs = input_without_bus(Event())
    with pytest.raises(ValueError, match="outside"):
        inputs.perform(
            "drag",
            {
                "x1": 10,
                "y1": 10,
                "x2": 900,
                "y2": 100,
                "button": "left",
                "modifier": ["ctrl"],
                "duration": 1,
            },
        )
    assert all(method != "NotifyPointerButton" for method, _ in portal_calls)
    assert portal_calls[-1] == ("NotifyKeyboardKeycode", (KEYCODES["ctrl"], 0))


@pytest.mark.parametrize("arguments", [["key", "a"], ["screenshot", "/tmp/unused.png"]])
def test_disconnected_cli(arguments: list[str], tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path), *arguments],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert "disconnected" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "inputs.jsonl").exists()


def test_stale_attach_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with tempfile.TemporaryDirectory(prefix="fw-stale-") as runtime:
        missing = Path(runtime) / "gone"
    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "kind": "attached",
                "runtime_directory": str(missing),
            }
        )
    )
    assert request_attached(tmp_path, "detach", {}) == 1
    assert "disconnected" in capsys.readouterr().err


_SERVER = """
import sys
import time
from pathlib import Path
import framewisp.attach as attach
from gi.repository import Gio
test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
test_bus.up()
root = Path(sys.argv[1])
class Portal:
    def __init__(self, stop):
        self.stop = stop
        self.size = (800, 600)
        self.node = 1
        self.closed = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    def open(self):
        (root / 'waiting').touch()
        while not (root / 'approve').exists():
            if self.stop.wait(.01):
                raise InterruptedError('cancelled')
    def input(self, method, signature, *values):
        if (root / 'fail').exists():
            raise RuntimeError('injected portal failure')
        with (root / 'events').open('a') as out:
            out.write(repr((method, values)) + '\\n')
    def close(self):
        if not self.closed:
            self.closed = True
            self.bus.close_sync(None)
            time.sleep(.1)
            (root / 'closed').touch()
attach.DesktopPortal = Portal
if len(sys.argv) > 2:
    import fcntl, termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    raise SystemExit(attach.attach_session(root / 'session'))
raise SystemExit(attach.run_attachment(root / 'session', None, retain_input_content=(root / 'retain').exists()))
"""


def wait_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, f"Timed out waiting for {path}"
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def private_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="fw-attach-", dir="/tmp") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        # Never send X11 releases to the developer's real display.
        monkeypatch.setenv("WAYLAND_DISPLAY", "framewisp-test-unused")
        yield Path(directory)


@pytest.fixture
def x11_desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make attach treat the desktop as X11, with an xdotool that logs releases."""
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "releases"
    xdotool = tools / "xdotool"
    xdotool.write_text(
        f'#!/bin/sh\n[ -e "{tmp_path}/xdotool-fails" ] && exit 1\n'
        f'echo "$*" >> "{log}"\n'
    )
    xdotool.chmod(0o755)
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("DISPLAY", ":framewisp-test")
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    return log


def releases(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def wait_releases(log: Path, count: int) -> list[str]:
    deadline = time.monotonic() + 5
    while len(releases(log)) < count:
        assert time.monotonic() < deadline, f"Expected {count} releases"
        time.sleep(0.01)
    return releases(log)


# R-ATTACH-1
def test_background_process_group_cannot_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Stubs model a background job; no terminal is allocated.
    def background_group(_descriptor: int) -> int:
        return os.getpgrp() + 1

    def is_terminal(_descriptor: int) -> bool:
        return True

    monkeypatch.setattr(os, "isatty", is_terminal)
    monkeypatch.setattr(os, "tcgetpgrp", background_group)
    monkeypatch.setattr(sys, "stdin", Mock(isatty=lambda: True, fileno=lambda: 0))
    monkeypatch.setattr(sys, "stdout", Mock(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError))
    assert attach_session(tmp_path / "session") == 1
    assert "foreground interactive terminal" in capsys.readouterr().err
    assert not (tmp_path / "session").exists()


# R-ATTACH-2
@pytest.mark.parametrize("target", ["runtime", "framewisp"])
def test_attach_refuses_shared_desktop_directory(
    private_runtime: Path, target: str
) -> None:
    directory = private_runtime / "framewisp"
    directory.mkdir(mode=0o700)
    shared = private_runtime if target == "runtime" else directory
    shared.chmod(0o755)
    try:
        with pytest.raises(RuntimeError, match="private"), reserve_desktop():
            pass
    finally:
        shared.chmod(0o700)
    assert not (directory / "desktop.lock").exists()


# R-ATTACH-2
@pytest.mark.parametrize(
    "target,message",
    [
        ("runtime", "XDG_RUNTIME_DIR must be owned by you"),
        ("framewisp", "must be a private directory owned by you"),
    ],
)
def test_attach_refuses_desktop_directory_of_another_user(
    private_runtime: Path, target: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = private_runtime / "framewisp"
    directory.mkdir(mode=0o700)
    if target == "framewisp":
        monkeypatch.setattr("framewisp.desktop.desktop_directory", lambda: directory)
    other = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: other)
    with pytest.raises(RuntimeError, match=message), reserve_desktop():
        pass
    assert not (directory / "desktop.lock").exists()


@pytest.fixture
def attach_process(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[subprocess.Popen[str]]:
    if getattr(request, "param", False):
        (tmp_path / "retain").touch()
    process = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_file(tmp_path / "waiting")
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("attach_process", [False, True], indirect=True)
def test_attached_cli_input_retention(
    tmp_path: Path, attach_process: subprocess.Popen[str]
) -> None:
    (tmp_path / "approve").touch()
    assert attach_process.stdout is not None
    while "Attached:" not in attach_process.stdout.readline():
        assert attach_process.poll() is None
    session = tmp_path / "session"
    retain = json.loads((session / "session.json").read_text())["retain_input_content"]
    for arguments in [
        ("type", "--interval", "0", "café 日本語 😀"),
        ("key", "Shift+a"),
        ("key", "Ctrl+a"),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", "framewisp", str(session), *arguments],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
    raw = (session / "inputs.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    assert ("café 日本語 😀" in raw) is retain
    assert ("Shift+a" in raw) is retain
    assert "Ctrl+a" in raw
    assert all(event["returncode"] == 0 for event in events if event["event"] == "end")
    (tmp_path / "fail").touch()
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(session), "type", "secret"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    raw = (session / "inputs.jsonl").read_text()
    assert ('"text": "secret"' in raw) is retain
    assert attach_process.wait(timeout=5) == 1


def test_pending_consent_reserves_session(
    tmp_path: Path,
    attach_process: subprocess.Popen[str],
) -> None:
    metadata = tmp_path / "session" / "session.json"
    original = metadata.read_text()
    duplicate = subprocess.run(
        [sys.executable, "-c", _SERVER, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert duplicate.returncode == 1
    assert "still live" in duplicate.stderr
    assert metadata.read_text() == original
    assert attach_process.poll() is None
    assert detach_desktop() == 0
    assert not metadata.exists()
    assert (tmp_path / "closed").exists()
    assert attach_process.wait(timeout=5) == 0


def test_detach_cancels_long_input_and_waits_for_cleanup(
    tmp_path: Path,
    attach_process: subprocess.Popen[str],
) -> None:
    session = tmp_path / "session"
    assert request_attached(session, "key", {"chord": "a"}) == 1
    assert not (tmp_path / "events").exists()
    (tmp_path / "approve").touch()
    assert attach_process.stdout is not None
    while "Attached:" not in attach_process.stdout.readline():
        assert attach_process.poll() is None
    command = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(session),
            "type",
            "AB",
            "--interval",
            "60",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_file(tmp_path / "events")
        started = time.monotonic()
        assert detach_desktop() == 0
        assert time.monotonic() - started < 3
        assert command.wait(timeout=5) == 1
        assert (tmp_path / "closed").exists()
        assert not (session / "session.json").exists()
        assert "66" not in (tmp_path / "events").read_text()
        assert attach_process.wait(timeout=5) == 0
    finally:
        if command.poll() is None:
            command.terminate()
        command.wait(timeout=5)


# R-ATTACH-1
def test_attach_requires_user_terminal(tmp_path: Path, private_runtime: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", str(tmp_path / "session"), "attach"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "Agents: do not allocate a PTY" in result.stderr
    assert not (tmp_path / "session").exists()
    assert not (private_runtime / "framewisp").exists()


def test_global_detach_cli_is_idempotent() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "framewisp", "--detach"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0
    assert "No active" in result.stdout


def test_other_session_cannot_take_desktop(
    tmp_path: Path, attach_process: subprocess.Popen[str]
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", _SERVER, str(other)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert not (other / "waiting").exists()
    assert attach_process.poll() is None


def test_global_detach_kills_suspended_owner_and_recovers(
    tmp_path: Path,
    attach_process: subprocess.Popen[str],
) -> None:
    attach_process.send_signal(signal.SIGSTOP)
    start = time.monotonic()
    assert detach_desktop() == 0
    assert time.monotonic() - start < 3
    assert attach_process.wait(timeout=5) == -signal.SIGKILL
    # SIGKILL cannot remove files. The next owner must recover without trusting them.
    original = json.loads((tmp_path / "session/session.json").read_text())["attachment"]
    replacement = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            json.loads((tmp_path / "session/session.json").read_text())["attachment"]
            == original
        ):
            assert replacement.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert detach_desktop() == 0
        assert replacement.wait(timeout=5) == 0
    finally:
        if replacement.poll() is None:
            replacement.kill()
        replacement.wait(timeout=5)


# R-INPUT-5
def test_x11_stop_releases_held_input(
    tmp_path: Path, x11_desktop: Path, attach_process: subprocess.Popen[str]
) -> None:
    # The guard proves it can release before the portal opens.
    assert releases(x11_desktop) == [" ".join(RELEASE[1:])]
    (tmp_path / "approve").touch()
    assert attach_process.stdout is not None
    while "Attached:" not in attach_process.stdout.readline():
        assert attach_process.poll() is None
    assert detach_desktop() == 0
    assert attach_process.wait(timeout=5) == 0
    # Normal shutdown waits for the release before it reports detachment.
    assert releases(x11_desktop) == [" ".join(RELEASE[1:])] * 2


# R-INPUT-5
def test_x11_release_survives_killing_the_owner_job(
    tmp_path: Path, x11_desktop: Path
) -> None:
    # Ctrl+C and terminal hangup reach the whole foreground job, and SIGKILL
    # allows no cleanup. The guard runs in its own session.
    owner = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        wait_file(tmp_path / "waiting")
        (tmp_path / "approve").touch()
        assert owner.stdout is not None
        while "Attached:" not in owner.stdout.readline():
            assert owner.poll() is None
        os.killpg(owner.pid, signal.SIGKILL)
        assert owner.wait(timeout=5) == -signal.SIGKILL
        assert wait_releases(x11_desktop, 2)[1] == " ".join(RELEASE[1:])
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)


@pytest.mark.parametrize("problem", ["xdotool fails", "no DISPLAY"])
def test_x11_attach_refuses_without_release(
    tmp_path: Path,
    x11_desktop: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    if problem == "no DISPLAY":
        monkeypatch.delenv("DISPLAY")
    else:
        (tmp_path / "xdotool-fails").touch()

    def no_portal(stop: Event) -> None:
        raise AssertionError("The portal opened without X11 input release.")

    monkeypatch.setattr(attach, "DesktopPortal", no_portal)
    assert run_attachment(tmp_path / "session", None) == 1
    error = capsys.readouterr().err
    assert ("DISPLAY" if problem == "no DISPLAY" else "xdotool") in error


def test_old_session_cannot_control_new_attachment(
    tmp_path: Path, attach_process: subprocess.Popen[str]
) -> None:
    old = tmp_path / "old"
    old.mkdir()
    (old / "session.json").write_text((tmp_path / "session/session.json").read_text())
    state = json.loads((old / "session.json").read_text())
    state["attachment"] = "previous-owner"
    (old / "session.json").write_text(json.dumps(state))
    assert request_attached(old, "key", {"chord": "a"}) == 1
    assert not (tmp_path / "events").exists()
    assert attach_process.poll() is None


def control_reply(session: Path, message: bytes) -> dict[str, object]:
    state = json.loads((session / "session.json").read_text())
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(str(Path(state["runtime_directory"]) / "control.sock"))
        connection.sendall(message)
        connection.shutdown(socket.SHUT_WR)
        return cast(dict[str, object], json.loads(connection.makefile().readline()))


# R-CONTROL-1, R-CONTROL-2
def test_malformed_requests_leave_the_attachment_running(
    tmp_path: Path, attach_process: subprocess.Popen[str]
) -> None:
    (tmp_path / "approve").touch()
    assert attach_process.stdout is not None
    while "Attached:" not in attach_process.stdout.readline():
        assert attach_process.poll() is None
    session = tmp_path / "session"
    token = json.loads((session / "session.json").read_text())["attachment"]
    os.mkfifo(tmp_path / "fifo")
    requests: list[dict[str, object]] = [
        {},
        {"action": 1, "parameters": {}},
        {"action": "key"},
        {"action": "key", "parameters": []},
        {"action": "key", "parameters": {}},
        {"action": "key", "parameters": {"chord": []}},
        {"action": "key", "parameters": {"chord": "\ud800"}},
        {"action": "unknown", "parameters": {}},
        {"action": "record-start", "parameters": {}},
        {"action": "click", "parameters": {"x": "a", "y": 0}},
        {"action": "screenshot"},
        {"action": "screenshot", "parameters": {"path": 1}},
        {"action": "screenshot", "parameters": {"path": "/tmp/nul\0.png"}},
        {"action": "screenshot", "parameters": {"path": "/tmp/\ud800.png"}},
        {"action": "screenshot", "parameters": {"path": "x.png"}},
        {"action": "screenshot", "parameters": {"path": ""}},
        {"action": "screenshot", "parameters": {"path": str(tmp_path)}},
        {"action": "screenshot", "parameters": {"path": str(tmp_path / "fifo")}},
        {"action": "screenshot", "parameters": {"path": "/dev/full"}},
        {
            "action": "screenshot",
            "parameters": {"path": str(tmp_path / "missing" / "x.png")},
        },
        {
            "action": "screenshot",
            "parameters": {"path": str(tmp_path / ("a" * 300))},
        },
    ]
    for message in (
        b"",
        b"\n",
        b"not json\n",
        b"\xff\n",
        b"[]\n",
        b"[" * 100000 + b"\n",
        *((json.dumps({"attachment": token} | r) + "\n").encode() for r in requests),
    ):
        response = control_reply(session, message)
        assert response["status"] == 1
        assert isinstance(response["error"], str) and response["error"]
        assert attach_process.poll() is None
    assert not (tmp_path / "events").exists()
    assert request_attached(session, "key", {"chord": "a"}) == 0
    assert (tmp_path / "events").exists()
    click: dict[str, object] = {"x": 10, "y": 20, "button": "left", "count": 1}
    assert request_attached(session, "click", click) == 0
    assert attach_process.poll() is None


# R-CONTROL-1, R-CONTROL-2
def test_trickling_client_gets_a_reply_from_the_attachment(
    tmp_path: Path, attach_process: subprocess.Popen[str]
) -> None:
    (tmp_path / "approve").touch()
    assert attach_process.stdout is not None
    while "Attached:" not in attach_process.stdout.readline():
        assert attach_process.poll() is None
    session = tmp_path / "session"
    state = json.loads((session / "session.json").read_text())
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(str(Path(state["runtime_directory"]) / "control.sock"))
        started = time.monotonic()
        while not select.select([connection], [], [], 0.3)[0]:
            # Without a total deadline, this client is never answered.
            assert time.monotonic() - started < 10
            connection.sendall(b" ")
        assert "Timed out waiting" in json.loads(connection.recv(4096))["error"]
    assert request_attached(session, "key", {"chord": "a"}) == 0
    assert attach_process.poll() is None


def test_screenshot_reaches_a_descriptor(tmp_path: Path) -> None:
    clip = tmp_path / "\udc80.png"
    with clip.open("wb") as output:
        write_png(
            "videotestsrc num-buffers=1", output.fileno(), Event(), tmp_path / "log"
        )
    assert clip.read_bytes().startswith(b"\x89PNG")


# R-ATTACH-7
def test_screenshot_skips_a_leftover_first_frame(tmp_path: Path) -> None:
    # Mutter can deliver an old frame first; the ball moves in every later frame.
    caps = "video/x-raw,width=64,height=48,framerate=30/1"
    first, newest = tmp_path / "first.png", tmp_path / "newest.png"
    for path, source in [
        (first, f"videotestsrc pattern=ball num-buffers=1 ! {caps}"),
        (newest, f"videotestsrc pattern=ball is-live=true num-buffers=30 ! {caps}"),
    ]:
        with path.open("wb") as output:
            write_png(source, output.fileno(), Event(), tmp_path / "log")
    assert first.read_bytes().startswith(b"\x89PNG")
    assert newest.read_bytes() != first.read_bytes()


# R-CONTROL-2
def test_screenshot_write_failure_fails_only_that_command(tmp_path: Path) -> None:
    # The log's directory is missing, as a full disk would refuse the log too.
    log = tmp_path / "missing" / "log"
    with open("/dev/full", "wb") as output:
        with pytest.raises(ValueError, match="Could not write"):
            write_png("videotestsrc num-buffers=1", output.fileno(), Event(), log)


class CapturePortal:
    node = 1

    def capture_fd(self) -> int:
        return os.open(os.devnull, os.O_RDONLY)


# R-CONTROL-2
def test_truncate_failure_fails_only_that_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    close = os.close

    def refuse(descriptor: int, length: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    def refuse_close(descriptor: int) -> None:
        close(descriptor)
        closed.append(descriptor)
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "ftruncate", refuse)
    monkeypatch.setattr(os, "close", refuse_close)
    with pytest.raises(ValueError, match="Cannot save screenshot"):
        open_destination(tmp_path / "x.png")
    monkeypatch.undo()
    assert len(closed) == 1


# R-CONTROL-2
@pytest.mark.parametrize("failure", [None, RuntimeError("capture failed")])
def test_failure_closing_the_destination_fails_only_that_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    written: list[int] = []
    close = os.close

    def fail_on_output(descriptor: int) -> None:
        close(descriptor)
        if descriptor in written:
            # The number is free for reuse once closed.
            written.remove(descriptor)
            raise OSError(errno.EDQUOT, "Disk quota exceeded")

    def write(source: str, output: int, stop: Event, log: Path) -> None:
        written.append(output)
        if failure is not None:
            raise failure

    monkeypatch.setattr(attach, "write_png", write)
    monkeypatch.setattr(os, "close", fail_on_output)
    portal = cast(DesktopPortal, CapturePortal())
    # A fatal capture failure stays fatal, whatever closing the file does.
    expected = RuntimeError if failure is not None else ValueError
    with pytest.raises(expected):
        attach.capture(portal, tmp_path / "x.png", Event(), tmp_path / "log")


# R-CONTROL-2
@pytest.mark.parametrize("interval", [1e10, 1e300, 2**70])
def test_huge_attached_wait_is_cancellable(interval: float) -> None:
    stop = Event()
    Timer(0.05, stop.set).start()
    with pytest.raises(InterruptedError):
        input_without_bus(stop).wait(interval)


def test_portal_failure_closes_owner(
    tmp_path: Path, attach_process: subprocess.Popen[str]
) -> None:
    (tmp_path / "approve").touch()
    assert attach_process.stdout is not None
    while "Attached:" not in attach_process.stdout.readline():
        assert attach_process.poll() is None
    (tmp_path / "fail").touch()
    assert request_attached(tmp_path / "session", "key", {"chord": "a"}) == 1
    assert attach_process.wait(timeout=5) == 1
    assert (tmp_path / "closed").exists()
    assert not (tmp_path / "session/session.json").exists()


@pytest.fixture
def terminal_prompt(
    tmp_path: Path,
) -> Iterator[tuple[subprocess.Popen[bytes], int, bytes]]:
    """Yield an owner waiting at the confirmation prompt and all it printed."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(tmp_path), "terminal"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    try:
        yield process, master, read_terminal(master, b"Type ATTACH")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        try:
            os.close(master)
        except OSError:
            pass


@pytest.fixture
def terminal_process(
    terminal_prompt: tuple[subprocess.Popen[bytes], int, bytes],
) -> tuple[subprocess.Popen[bytes], int]:
    return terminal_prompt[:2]


def read_terminal(descriptor: int, expected: bytes) -> bytes:
    output = b""
    deadline = time.monotonic() + 5
    while expected not in output:
        assert time.monotonic() < deadline, output
        ready, _, _ = select.select([descriptor], [], [], 0.05)
        if ready:
            output += os.read(descriptor, 4096)
    return output


# R-ATTACH-5
def test_terminal_names_stop_methods_before_confirmation(
    tmp_path: Path,
    terminal_prompt: tuple[subprocess.Popen[bytes], int, bytes],
) -> None:
    _process, _master, printed = terminal_prompt
    assert b"framewisp --detach" in printed
    assert b"Ctrl+C" in printed
    assert not (tmp_path / "waiting").exists()


# R-ATTACH-1
@pytest.mark.parametrize("confirmation", [b"yes\n", b"\x04"])
def test_terminal_requires_exact_confirmation(
    tmp_path: Path,
    terminal_process: tuple[subprocess.Popen[bytes], int],
    confirmation: bytes,
) -> None:
    process, master = terminal_process
    os.write(master, confirmation)
    assert process.wait(timeout=5) == 1
    assert not (tmp_path / "waiting").exists()
    assert not (tmp_path / "session").exists()


@pytest.mark.parametrize(
    "sig", [signal.SIGHUP, signal.SIGTSTP, signal.SIGINT, signal.SIGTERM]
)
def test_terminal_owner_stops_on_exit_signals(
    tmp_path: Path,
    terminal_process: tuple[subprocess.Popen[bytes], int],
    sig: signal.Signals,
) -> None:
    process, master = terminal_process
    os.write(master, b"ATTACH\n")
    wait_file(tmp_path / "waiting")
    (tmp_path / "approve").touch()
    read_terminal(master, b"Attached:")
    process.send_signal(sig)
    assert process.wait(timeout=5) == 0
    assert (tmp_path / "closed").exists()
    assert not (tmp_path / "session/session.json").exists()


def test_closing_terminal_disconnects(
    tmp_path: Path,
    terminal_process: tuple[subprocess.Popen[bytes], int],
) -> None:
    process, master = terminal_process
    os.write(master, b"ATTACH\n")
    wait_file(tmp_path / "waiting")
    (tmp_path / "approve").touch()
    read_terminal(master, b"Attached:")
    os.close(master)
    process.wait(timeout=5)
    assert (tmp_path / "closed").exists()
    assert not (tmp_path / "session/session.json").exists()


def test_detach_needs_only_standard_library() -> None:
    script = """
import sys
class NoDesktopImports:
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'gi', 'vncdotool', 'PIL'}:
            raise RuntimeError('Emergency stop imported ' + fullname)
sys.meta_path.insert(0, NoDesktopImports())
sys.argv = ['framewisp', '--detach']
from framewisp.__main__ import main
main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("state", ["{", "[]", '{"kind": "attached"}'])
def test_invalid_session_metadata_fails_clearly(
    tmp_path: Path, state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "session.json").write_text(state)
    assert request_attached(tmp_path, "key", {"chord": "a"}) == 1
    assert "Cannot use attached session" in capsys.readouterr().err


def test_attachment_refuses_headless_ownership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    with session_lease(session) as lease:
        assert run_attachment(session, None) == 1
        assert "still live" in capsys.readouterr().err
        write_journal(session, runtime_path(session), lease, created=False)
    assert run_attachment(session, None) == 1
    assert "recover first" in capsys.readouterr().err
    assert not (session / "session.json").exists()
    recover_session(session)


def test_recovery_refuses_live_attachment(
    attach_process: subprocess.Popen[str], tmp_path: Path
) -> None:
    state = tmp_path / "session" / "session.json"
    before = state.read_bytes()
    with pytest.raises(SessionError, match="still live"):
        recover_session(tmp_path / "session")
    assert state.read_bytes() == before
    assert attach_process.poll() is None

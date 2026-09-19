"""Cancellation tests replace portal I/O; live consent is tested on the desktop."""

import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import pytest

from framewisp.attach import KEYCODES, AttachedInput
from framewisp.connection import request_attached
from framewisp.desktop import detach_desktop
from framewisp.portal import DesktopPortal


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
root = Path(sys.argv[1])
class Portal:
    def __init__(self, stop):
        self.stop = stop
        self.size = (800, 600)
        self.node = 1
        self.closed = False
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
            time.sleep(.1)
            (root / 'closed').touch()
attach.DesktopPortal = Portal
attach.dispatch_events = lambda: None
if len(sys.argv) > 2:
    import fcntl, termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    raise SystemExit(attach.attach_session(root / 'session'))
raise SystemExit(attach.run_attachment(root / 'session', None))
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
        yield Path(directory)


@pytest.fixture
def attach_process(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
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
    assert "already exists" in duplicate.stderr
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
def terminal_process(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], int]]:
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
        read_terminal(master, b"Type ATTACH")
        yield process, master
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        try:
            os.close(master)
        except OSError:
            pass


def read_terminal(descriptor: int, expected: bytes) -> None:
    output = b""
    deadline = time.monotonic() + 5
    while expected not in output:
        assert time.monotonic() < deadline, output
        ready, _, _ = select.select([descriptor], [], [], 0.05)
        if ready:
            output += os.read(descriptor, 4096)


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

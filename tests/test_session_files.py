"""One check keeps recordings and captures away from what a session owns."""

import ast
import re
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest
from test_integration import Demo, cli, demo

from framewisp import lib
from framewisp.console import ConsoleCapture
from framewisp.errors import SessionError
from framewisp.lib import (
    Recordings,
    active_recording,
    batch_screenshot,
    capture_path_error,
    parse_batch,
    recording_path_error,
)
from framewisp.ownership import ACTIVE_RECORDING, JOURNAL, LOCK
from framewisp.session_files import (
    SESSION_FILES,
    protected_path_error,
    resolved_or_unchanged,
)

__all__ = ["demo"]

ACTION = {"action": "move", "x": 0, "y": 0}


def batch(recordings: Recordings, path: Path) -> object:
    return parse_batch(
        {"actions": [ACTION], "capture": {"path": str(path)}}, recordings
    )


@pytest.fixture
def recordings(tmp_path: Path) -> Recordings:
    return Recordings(tmp_path, {}, Event(), (1280, 720), ConsoleCapture(tmp_path))


SESSION_ROOTS = {"session", "self.session", "capture.session", "log.parent"}
# Session paths not spelled as a literal, and the names each one can produce.
COMPUTED_NAMES = {
    "LOCK": {LOCK},
    "JOURNAL": {JOURNAL},
    "ACTIVE_RECORDING": {ACTIVE_RECORDING},
    # Loops over fixed tuples, left to the test that lists a real session.
    "m": set[str](),
    "name": set[str](),
    "name + '.log'": {
        f"{name}.log"
        for name in (
            "dbus",
            "accessibility",
            "registry",
            "sway",
            "wayvnc",
            "recorder",
            "app",
        )
    },
}
LOG_NAME = re.compile(r"\.?[\w.-]+\.(log|jsonl)")


def names_in_source(tree: ast.Module) -> set[str]:
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if LOG_NAME.fullmatch(node.value):
                named.add(node.value)
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and ast.unparse(node.left) in SESSION_ROOTS
        ):
            if isinstance(node.right, ast.Constant):
                named.add(str(node.right.value))
            else:
                expression = ast.unparse(node.right)
                assert expression in COMPUTED_NAMES, (
                    f"Add the names {expression} can produce to SESSION_FILES "
                    "and COMPUTED_NAMES."
                )
                named |= COMPUTED_NAMES[expression]
    return named


# R-SESSION-7
def test_every_session_path_in_the_source_is_listed() -> None:
    """Covers files no test session creates, such as the attached capture log."""
    source = Path(lib.__file__).parent
    named: set[str] = set()
    for module in source.glob("*.py"):
        named |= names_in_source(ast.parse(module.read_text()))
    assert "capture.log" in named
    assert named - set(SESSION_FILES) == set()


# R-SESSION-7
def test_capture_log_is_reserved(recordings: Recordings, tmp_path: Path) -> None:
    path = tmp_path / "capture.log"
    with pytest.raises(ValueError, match="reserved for session files"):
        batch(recordings, path)
    assert "reserved" in str(recording_path_error(tmp_path, path))


# R-SESSION-7
def test_batch_capture_through_a_hard_link_is_reserved(
    recordings: Recordings, tmp_path: Path
) -> None:
    (tmp_path / "session.json").write_text("{}")
    linked = tmp_path / "linked.png"
    linked.hardlink_to(tmp_path / "session.json")
    with pytest.raises(ValueError, match="reserved for session files"):
        batch(recordings, linked)


# R-REC-1
def test_batch_capture_still_refuses_an_ordinary_existing_file(
    recordings: Recordings, tmp_path: Path
) -> None:
    existing = tmp_path / "existing.png"
    existing.touch()
    with pytest.raises(ValueError, match="already exists.*capture path"):
        batch(recordings, existing)


def test_replaced_metadata_leaves_a_stale_hard_link_unprotected(tmp_path: Path) -> None:
    state = tmp_path / "session.json"
    state.write_text("old")
    stale = tmp_path / "stale.png"
    stale.hardlink_to(state)
    replacement = tmp_path / ".session.json"
    replacement.write_text("new")
    replacement.replace(state)
    assert protected_path_error(tmp_path, stale, None) is None


# R-REC-21
def test_screenshot_reads_the_active_destination_from_session_state(
    tmp_path: Path,
) -> None:
    clip = tmp_path / "clip.mp4"
    active = active_recording({"recording": str(clip)})
    assert "active recording" in str(capture_path_error(tmp_path, clip, active))
    assert capture_path_error(tmp_path, tmp_path / "other.png", active) is None
    assert capture_path_error(tmp_path, Path("-"), active) is None


# R-REC-21
def test_batch_capture_rechecks_a_recording_started_after_validation(
    recordings: Recordings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_capture(*arguments: object) -> int:
        return 0

    monkeypatch.setattr(lib, "grim", no_capture)
    (tmp_path / "session.json").write_text("{}")
    clip = tmp_path / "clip.mp4"
    recordings.destination = clip
    with pytest.raises(SessionError, match="active recording"):
        batch_screenshot(tmp_path, clip, lambda: False, recordings)
    assert not clip.exists()
    assert not list(tmp_path.glob(".framewisp-capture-*"))


# R-SESSION-7
def test_recording_through_a_hard_link_is_reserved(tmp_path: Path) -> None:
    (tmp_path / "session.json").write_text("{}")
    alias = tmp_path / "alias.mp4"
    alias.hardlink_to(tmp_path / "session.json")
    assert "reserved" in str(recording_path_error(tmp_path, alias))


# R-SESSION-7
def test_recording_through_a_symlink_is_reserved(tmp_path: Path) -> None:
    session = tmp_path / "session"
    alias = tmp_path / "alias.mp4"
    alias.symlink_to(session / "session.json")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "framewisp",
            str(session),
            "run",
            "--record",
            str(alias),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode != 0
    assert "reserved for session files" in result.stderr
    assert not session.exists()


def test_a_looped_link_resolves_to_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before Python 3.13, `resolve` raises on a loop."""

    def looped(self: Path) -> Path:
        raise RuntimeError("Symlink loop")

    monkeypatch.setattr(Path, "resolve", looped)
    link = tmp_path / ACTIVE_RECORDING
    assert resolved_or_unchanged(link) == link


# R-SESSION-7
def test_session_from_an_older_runner_keeps_screenshots_working(
    tmp_path: Path,
) -> None:
    """Without the key only the active destination goes unchecked."""
    active = active_recording({})
    assert active is None
    assert capture_path_error(tmp_path, tmp_path / "clip.mp4", active) is None
    error = capture_path_error(tmp_path, tmp_path / "session.json", active)
    assert "reserved" in str(error)


# R-SESSION-7
@pytest.mark.integration
@pytest.mark.parametrize("demo", [True], indirect=True)
def test_a_session_creates_only_listed_files(demo: Demo) -> None:
    cli(demo.directory, "click", "120", "100")
    cli(demo.directory, "type", "listed")
    cli(demo.directory, "record-stop")
    during = {path.name for path in demo.directory.iterdir() if not path.is_dir()}
    cli(demo.directory, "stop")
    assert demo.process.wait(timeout=20) == 0
    after = {path.name for path in demo.directory.iterdir() if not path.is_dir()}
    assert (during | after) - set(SESSION_FILES) == set()

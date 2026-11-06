"""One check keeps recordings and captures away from what a session owns."""

import ast
from pathlib import Path
from threading import Event

import pytest
from test_integration import Demo, cli, demo

from framewisp import lib
from framewisp.console import ConsoleCapture
from framewisp.lib import (
    Recordings,
    parse_batch,
    recording_path_error,
    screenshot_path_error,
)
from framewisp.session_files import SESSION_FILES, protected_path_error

__all__ = ["demo"]

ACTION = {"action": "move", "x": 0, "y": 0}


def batch(recordings: Recordings, path: Path) -> object:
    return parse_batch(
        {"actions": [ACTION], "capture": {"path": str(path)}}, recordings
    )


@pytest.fixture
def recordings(tmp_path: Path) -> Recordings:
    return Recordings(tmp_path, {}, Event(), (1280, 720), ConsoleCapture(tmp_path))


# R-SESSION-7
def test_every_session_path_in_the_source_is_listed() -> None:
    """Catches files no test session creates, such as the attached capture log."""
    source = Path(lib.__file__).parent
    named: set[str] = set()
    for module in source.glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant)
                and ast.unparse(node.left)
                in {"session", "self.session", "capture.session", "log.parent"}
            ):
                named.add(str(node.right.value))
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
    state = {"recording": str(clip)}
    assert "active recording" in str(screenshot_path_error(tmp_path, clip, state))
    assert screenshot_path_error(tmp_path, tmp_path / "other.png", state) is None
    assert screenshot_path_error(tmp_path, Path("-"), state) is None


# R-SESSION-7
def test_session_from_an_older_runner_keeps_screenshots_working(
    tmp_path: Path,
) -> None:
    """Without the key only the active destination goes unchecked."""
    state: dict[str, object] = {}
    assert screenshot_path_error(tmp_path, tmp_path / "clip.mp4", state) is None
    error = screenshot_path_error(tmp_path, tmp_path / "session.json", state)
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

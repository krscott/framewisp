"""Recording behaviour that needs no display."""

import errno
from pathlib import Path
from threading import Event

import pytest

from framewisp.console import ConsoleCapture
from framewisp.lib import Recordings
from framewisp.ownership import ACTIVE_RECORDING, interrupted_recording


# R-REC-19
def test_filesystem_without_hard_links_rejects_recording_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mounting a filesystem without hard links would need root.
    def refuse(self: Path, target: Path) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(Path, "hardlink_to", refuse)
    session = tmp_path / "session"
    session.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    recordings = Recordings(
        session, {}, Event(), (1280, 720), ConsoleCapture(session)
    )
    error = recordings.start(output / "clip.mp4")
    assert error is not None
    assert "hard links" in error
    assert "another filesystem" in error
    assert recordings.process is None
    assert list(output.iterdir()) == []
    assert not (session / "recorder.log").exists()


def test_recover_ignores_a_link_to_a_removed_clip(tmp_path: Path) -> None:
    (tmp_path / ACTIVE_RECORDING).symlink_to(tmp_path / "removed" / "recording.mp4")
    assert interrupted_recording(tmp_path) is None
    assert not (tmp_path / ACTIVE_RECORDING).is_symlink()

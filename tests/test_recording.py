"""Recording behaviour that needs no display."""

import errno
import os
import subprocess
from pathlib import Path
from threading import Event

import pytest

from framewisp.console import ConsoleCapture
from framewisp.lib import Recordings
from framewisp.ownership import ACTIVE_RECORDING, interrupted_recording

# wf-recorder wrote this shape when stopped right after its first frame. To keep
# it small, this is a 16x16 one-frame FFmpeg MP4 with the mvhd, tkhd, mdhd, elst
# and stts durations zeroed: FFprobe reports no duration and FFmpeg decodes nothing.
ZERO_DURATION_CLIP = Path(__file__).with_name("zero_duration_clip.mp4")


def decoded_frames(path: Path) -> int:
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return sum(1 for line in decoded.stdout.splitlines() if not line.startswith("#"))


def recorded_by_fake_recorder(tmp_path: Path, written: Path) -> dict[str, object]:
    """Finish a clip whose recorder writes `written`, and return the summary."""
    tools = tmp_path / "tools"
    tools.mkdir()
    recorder = tools / "wf-recorder"
    recorder.write_text(f"""#!/bin/sh
while [ $# -gt 1 ]; do shift; done
cp '{written}' "$1"
trap 'exit 0' TERM
while :; do sleep 0.01; done
""")
    recorder.chmod(0o755)
    session = tmp_path / "session"
    session.mkdir()
    environment = os.environ | {"PATH": f"{tools}:{os.environ['PATH']}"}
    recordings = Recordings(
        session, environment, Event(), (1280, 720), ConsoleCapture(session)
    )
    destination = tmp_path / "clip.mp4"
    error = recordings.start(destination, captions="none", click_pings=False)
    assert error is None
    assert recordings.finish() is None
    assert destination.stat().st_size > 0
    assert not list(tmp_path.glob(".framewisp-recording-*"))
    summary = recordings.last_summary
    assert summary is not None
    return summary


# R-REC-3
def test_clip_with_a_hidden_frame_is_finalized_as_playable(tmp_path: Path) -> None:
    assert decoded_frames(ZERO_DURATION_CLIP) == 0
    summary = recorded_by_fake_recorder(tmp_path, ZERO_DURATION_CLIP)
    assert summary["duration_seconds"] == pytest.approx(1 / 30, abs=0.001)
    assert (summary["width"], summary["height"]) == (16, 16)
    assert decoded_frames(tmp_path / "clip.mp4") == 1


# R-REC-22
def test_clip_with_no_measurable_duration_reports_null(tmp_path: Path) -> None:
    written = tmp_path / "no-frames.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=16x16:r=30",
            "-t",
            "0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(written),
        ],
        check=True,
        timeout=10,
    )
    summary = recorded_by_fake_recorder(tmp_path, written)
    assert summary["duration_seconds"] is None
    assert summary["width"] is None
    assert summary["height"] is None
    assert summary["size_bytes"] == written.stat().st_size


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
    recordings = Recordings(session, {}, Event(), (1280, 720), ConsoleCapture(session))
    error = recordings.start(output / "clip.mp4")
    assert error is not None
    assert "hard links" in error
    assert "another filesystem" in error
    assert recordings.process is None
    assert list(output.iterdir()) == []
    assert not (session / "recorder.log").exists()


# R-REC-20
def test_recover_removes_directory_of_a_clip_never_written(tmp_path: Path) -> None:
    directory = tmp_path / ".framewisp-recording-unused"
    directory.mkdir()
    (tmp_path / ACTIVE_RECORDING).symlink_to(directory / "recording.mp4")
    assert interrupted_recording(tmp_path) is None
    assert not directory.exists()
    assert not (tmp_path / ACTIVE_RECORDING).is_symlink()


# R-REC-20
def test_recover_ignores_a_link_to_a_removed_directory(tmp_path: Path) -> None:
    (tmp_path / ACTIVE_RECORDING).symlink_to(tmp_path / "removed" / "recording.mp4")
    assert interrupted_recording(tmp_path) is None
    assert not (tmp_path / ACTIVE_RECORDING).is_symlink()

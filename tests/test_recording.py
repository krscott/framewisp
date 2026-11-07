"""Recording behaviour that needs no display."""

import errno
import os
import subprocess
from pathlib import Path
from threading import Event

import pytest

from framewisp.console import ConsoleCapture
from framewisp.layout import ConsolePanel
from framewisp.lib import Recordings
from framewisp.ownership import ACTIVE_RECORDING, interrupted_recording

# wf-recorder wrote this shape for a clip stopped right after record-start under
# load. To keep it small, this is a 16x16 one-frame FFmpeg MP4 with the mvhd, tkhd,
# mdhd, elst and stts durations zeroed: FFprobe reports no duration and FFmpeg
# decodes nothing.
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


def recorded_by_fake_recorder(
    tmp_path: Path,
    written: Path,
    *,
    compact: bool = False,
    console: ConsolePanel | None = None,
) -> dict[str, object]:
    """Finish a clip whose recorder writes `written`, and return the summary."""
    tools = tmp_path / "recorder-tools"
    tools.mkdir()
    recorder = tools / "wf-recorder"
    recorder.write_text(f"""#!/bin/sh
trap 'exit 0' TERM
while [ $# -gt 1 ]; do shift; done
echo '[1.0] zwlr_screencopy_frame_v1#5.ready(0, 1, 0)'
cp '{written}' "$1"
while :; do sleep 0.01; done
""")
    recorder.chmod(0o755)
    session = tmp_path / "session"
    session.mkdir()
    (session / "inputs.jsonl").touch()
    environment = os.environ | {"PATH": f"{tools}:{os.environ['PATH']}"}
    recordings = Recordings(
        session, environment, Event(), (1280, 720), ConsoleCapture(session)
    )
    destination = tmp_path / "clip.mp4"
    error = recordings.start(destination, compact=compact, console=console)
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
@pytest.mark.parametrize(
    ("compact", "console"),
    [(False, None), (True, None), (False, ConsolePanel())],
    ids=["plain", "compact", "console"],
)
def test_clip_with_no_measurable_duration_reports_null(
    tmp_path: Path, compact: bool, console: ConsolePanel | None
) -> None:
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
    summary = recorded_by_fake_recorder(
        tmp_path, written, compact=compact, console=console
    )
    assert summary["duration_seconds"] is None
    assert summary["width"] is None
    assert summary["height"] is None
    assert summary["size_bytes"] == written.stat().st_size


def fail_remux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    tools = tmp_path / "remux-tools"
    tools.mkdir()
    remuxer = tools / "ffmpeg"
    remuxer.write_text(f"#!/bin/sh\n{script}\nexit 1\n")
    remuxer.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")


# R-REC-22
def test_failed_frame_restore_is_logged_and_reports_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_remux(tmp_path, monkeypatch, "echo no space left >&2")
    summary = recorded_by_fake_recorder(tmp_path, ZERO_DURATION_CLIP)
    assert summary["duration_seconds"] is None
    assert (summary["width"], summary["height"]) == (16, 16)
    log = (tmp_path / "session" / "recorder.log").read_text()
    assert "no space left" in log


# R-REC-22
def test_failed_frame_restore_survives_an_unwritable_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "session" / "recorder.log"
    fail_remux(tmp_path, monkeypatch, f"ln -sf /dev/full '{log}'")
    summary = recorded_by_fake_recorder(tmp_path, ZERO_DURATION_CLIP)
    assert summary["duration_seconds"] is None


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

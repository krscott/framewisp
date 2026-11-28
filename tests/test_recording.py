"""Recording behaviour that needs no display."""

import errno
import os
import subprocess
import time
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest

from framewisp.console import ConsoleCapture
from framewisp.errors import SessionError
from framewisp.finalization import capture_origin
from framewisp.layout import ConsolePanel
from framewisp.lib import ClipProbe, Recording
from framewisp.recorder import filter_recorder_output, recorder_output
from framewisp.recovery import PRIVATE_CLIP, interrupted_clip

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


def started_by_fake_recorder(
    tmp_path: Path,
    written: Path,
    *,
    click_pings: bool = True,
    compact: bool = False,
    console_panel: ConsolePanel | None = None,
) -> Recording:
    """Start a clip at `tmp_path / "clip.mp4"` whose recorder writes `written`."""
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
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "inputs.jsonl").touch()
    environment = os.environ | {"PATH": f"{tools}:{os.environ['PATH']}"}
    recording = Recording(
        session_dir, environment, Event(), (1280, 720), ConsoleCapture(session_dir)
    )
    error = recording.start(
        tmp_path / "clip.mp4",
        click_pings=click_pings,
        compact=compact,
        console_panel=console_panel,
    )
    assert error is None
    return recording


def recorded_by_fake_recorder(
    tmp_path: Path,
    written: Path,
    *,
    compact: bool = False,
    console_panel: ConsolePanel | None = None,
) -> dict[str, object]:
    """Finish a clip whose recorder writes `written`, and return the summary."""
    recording = started_by_fake_recorder(
        tmp_path, written, compact=compact, console_panel=console_panel
    )
    assert recording.finish() is None
    assert (tmp_path / "clip.mp4").stat().st_size > 0
    assert not list(tmp_path.glob(".framewisp-clip-*"))
    summary = recording.last_summary
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
    ("compact", "console_panel"),
    [(False, None), (True, None), (False, ConsolePanel())],
    ids=["plain", "compact", "console"],
)
def test_clip_with_no_measurable_duration_reports_null(
    tmp_path: Path, compact: bool, console_panel: ConsolePanel | None
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
        tmp_path, written, compact=compact, console_panel=console_panel
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


def fail_restore(
    clip: Path, probe: ClipProbe, *, log: Path, env: dict[str, str]
) -> bool:
    raise RuntimeError("restore failed")


# R-REC-26
@pytest.mark.parametrize("click_pings", [True, False])
def test_truncated_clip_is_not_published_and_remains_at_private_path(
    tmp_path: Path, click_pings: bool
) -> None:
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(ZERO_DURATION_CLIP.read_bytes()[:64])
    recording = started_by_fake_recorder(tmp_path, truncated, click_pings=click_pings)
    with pytest.raises(SessionError) as failure:
        recording.finish()
    destination = tmp_path / "clip.mp4"
    assert not destination.exists()
    assert recording.last_summary is None
    kept_dirs = list(tmp_path.glob(".framewisp-clip-*"))
    assert len(kept_dirs) == 1
    kept = kept_dirs[0] / "clip.mp4"
    assert kept.read_bytes() == truncated.read_bytes()
    assert (tmp_path / "session" / PRIVATE_CLIP).readlink() == kept
    error = str(failure.value)
    assert "Could not inspect clip" in error
    assert f"The clip remains at {kept}." in error


# R-REC-10
@pytest.mark.parametrize("click_pings", [True, False])
def test_failed_restore_publishes_the_raw_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, click_pings: bool
) -> None:
    monkeypatch.setattr("framewisp.lib.restore_hidden_frames", fail_restore)
    recording = started_by_fake_recorder(
        tmp_path, ZERO_DURATION_CLIP, click_pings=click_pings
    )
    with pytest.raises(SessionError) as failure:
        recording.finish()
    clip = tmp_path / "clip.mp4"
    assert clip.read_bytes() == ZERO_DURATION_CLIP.read_bytes()
    assert not list(tmp_path.glob(".framewisp-clip-*"))
    error = str(failure.value)
    assert f"raw clip remains at {clip}" in error
    assert "restore failed" in error
    assert ("without overlays" in error) == click_pings


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
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    recording = Recording(
        session_dir, {}, Event(), (1280, 720), ConsoleCapture(session_dir)
    )
    error = recording.start(output / "clip.mp4")
    assert error is not None
    assert "hard links" in error
    assert "another filesystem" in error
    assert recording.process is None
    assert list(output.iterdir()) == []
    assert not (session_dir / "recorder.log").exists()


# R-REC-20
def test_recover_removes_directory_of_a_clip_never_written(tmp_path: Path) -> None:
    directory = tmp_path / ".framewisp-clip-unused"
    directory.mkdir()
    (tmp_path / PRIVATE_CLIP).symlink_to(directory / "clip.mp4")
    assert interrupted_clip(tmp_path) is None
    assert not directory.exists()
    assert not (tmp_path / PRIVATE_CLIP).is_symlink()


# R-REC-20
def test_recover_ignores_a_link_to_a_removed_directory(tmp_path: Path) -> None:
    (tmp_path / PRIVATE_CLIP).symlink_to(tmp_path / "removed" / "clip.mp4")
    assert interrupted_clip(tmp_path) is None
    assert not (tmp_path / PRIVATE_CLIP).is_symlink()


def test_recorder_filter_keeps_origin_and_diagnostics(tmp_path: Path) -> None:
    origin = b"[00:15:39.8] {Default Queue} zwlr_screencopy_frame_v1#6.ready(0, 123, 250000000)\n"
    later = b"[00:15:40.8] {Default Queue} zwlr_screencopy_frame_v1#7.ready(0, 124, 250000000)\n"
    diagnostic = b"[libx264 @ 0x123] encoding diagnostic\nUnable to open output file\n"
    trace = b"[00:15:39.7] {Default Queue}  -> wl_display#1.get_registry(new id wl_registry#2)\n"
    discarded = b"[00:15:40.9] {Default Queue} discarded wl_buffer#12.release()\n"
    output = BytesIO()
    filter_recorder_output(
        BytesIO(trace + origin + (later + discarded) * 10000 + diagnostic), output
    )
    assert output.getvalue() == origin + diagnostic
    log = tmp_path / "recorder.log"
    log.write_bytes(output.getvalue())
    assert capture_origin(log) == 123.25


@pytest.mark.parametrize("failure", [None, "cleanup failed: PID 123"])
def test_recorder_output_does_not_wait_forever_for_inherited_writer(
    tmp_path: Path, failure: str | None
) -> None:
    duplicate: int | None = None
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match=failure or "inherited log pipe"):
            with recorder_output(tmp_path / "recorder.log") as sink:
                duplicate = os.dup(sink.fileno())
                sink.write(b"partial diagnostic")
                sink.flush()
                if failure is not None:
                    raise RuntimeError(failure)
        assert time.monotonic() - started < 2
        assert (tmp_path / "recorder.log").read_bytes() == b"partial diagnostic"
    finally:
        if duplicate is not None:
            os.close(duplicate)

"""A demonstration bundle produced from a screen-only capture stream."""

# pyright: reportMissingModuleSource=false

import json
import time
from pathlib import Path
from typing import BinaryIO

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from framewisp.desktop import write_state


class Teaching:
    def __init__(self, directory: Path, goal: str):
        self.directory = directory
        self.goal = goal
        self.pipeline: Gst.Element | None = None
        self.frames: Gst.Element | None = None
        self.video: BinaryIO | None = None
        self.created = False
        self.count = 0
        self.started = 0.0

    def manifest(self, status: str, error: str | None = None) -> None:
        write_state(
            self.directory / "lesson.json",
            {
                "schema_version": 1,
                "goal": self.goal,
                "status": status,
                "video": "demonstration.webm",
                "timeline": "timeline.jsonl",
                "frame_count": self.count,
                "error": error,
            },
        )

    def start(self, source: str) -> None:
        Gst.init(None)
        # Atomic refusal includes existing directories, files and symlinks.
        self.directory.mkdir(mode=0o700)
        self.created = True
        (self.directory / "frames").mkdir(mode=0o700)
        (self.directory / "timeline.jsonl").touch(mode=0o600)
        self.manifest("recording")
        self.video = (self.directory / "demonstration.webm").open("xb")
        pipeline = Gst.parse_launch(
            f"{source} ! tee name=screen "
            "screen. ! queue ! videoconvert ! videorate drop-only=true ! "
            "video/x-raw,format=I420,framerate=15/1 ! "
            "vp8enc deadline=1 cpu-used=8 keyframe-max-dist=30 ! "
            "webmmux streamable=true ! "
            f"fdsink fd={self.video.fileno()} sync=false "
            "screen. ! queue leaky=downstream max-size-buffers=1 ! videoconvert ! "
            "videorate drop-only=true ! video/x-raw,framerate=1/1 ! "
            "pngenc ! appsink name=frames sync=false max-buffers=2 drop=true wait-on-eos=false"
        )
        assert isinstance(pipeline, Gst.Bin)
        self.pipeline = pipeline
        self.frames = pipeline.get_by_name("frames")
        self.started = time.monotonic()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Could not start teaching capture.")

    def append(self, entry: dict[str, object]) -> None:
        with (self.directory / "timeline.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")

    def note(self, text: str) -> None:
        assert self.pipeline is not None
        clock = self.pipeline.get_clock()
        if clock is None:
            raise ValueError("Teaching capture has not started yet. Try again shortly.")
        elapsed = (clock.get_time() - self.pipeline.get_base_time()) / Gst.SECOND
        self.append({"type": "note", "time": elapsed, "text": text})

    def drain(self) -> None:
        assert self.frames is not None
        while (sample := self.frames.emit("try-pull-sample", 0)) is not None:
            assert isinstance(sample, Gst.Sample)
            buffer = sample.get_buffer()
            segment = sample.get_segment()
            assert buffer is not None and segment is not None
            timestamp = segment.to_running_time(Gst.Format.TIME, buffer.pts)
            if timestamp == Gst.CLOCK_TIME_NONE:
                raise RuntimeError("Teaching capture delivered an untimed frame.")
            path = f"frames/{self.count:06d}.png"
            (self.directory / path).write_bytes(
                buffer.extract_dup(0, buffer.get_size())
            )
            self.append({"type": "frame", "time": timestamp / Gst.SECOND, "path": path})
            self.count += 1

    def poll(self) -> None:
        assert self.pipeline is not None
        self.drain()
        bus = self.pipeline.get_bus()
        assert bus is not None
        message = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is not None:
            if message.type == Gst.MessageType.ERROR:
                error, detail = message.parse_error()
                raise RuntimeError(f"Teaching capture failed: {error}; {detail}")
            raise RuntimeError("Teaching capture ended unexpectedly.")
        if self.count == 0 and time.monotonic() - self.started > 10:
            raise RuntimeError("Teaching capture received no frame within 10 seconds.")

    def close(self, error: str | None = None) -> None:
        """Finalize within a bounded time, preserving partial evidence on failure."""
        if not self.created:
            return
        try:
            if self.pipeline is not None:
                try:
                    self.drain()
                    bus = self.pipeline.get_bus()
                    assert bus is not None
                    self.pipeline.send_event(Gst.Event.new_eos())
                    message = bus.timed_pop_filtered(
                        Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
                    )
                    self.drain()
                    if message is None:
                        error = error or "Teaching video finalization timed out."
                    elif message.type == Gst.MessageType.ERROR:
                        failure, detail = message.parse_error()
                        error = (
                            error
                            or f"Teaching video finalization failed: {failure}; {detail}"
                        )
                finally:
                    self.pipeline.set_state(Gst.State.NULL)
            if self.count == 0:
                error = error or "Teaching capture saved no observations."
        except (OSError, RuntimeError, GLib.Error) as failure:
            error = error or str(failure)
        finally:
            if self.video is not None:
                self.video.close()
        self.manifest("failed" if error else "complete", error)
        if error is not None:
            raise RuntimeError(f"{error} Partial demonstration: {self.directory}")

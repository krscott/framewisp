"""Command-based recording cuts on the original monotonic timeline."""

import json
import math
import subprocess
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

# Seconds kept around each activity span and at each end of a clip. A click ping
# starts inside its input's span, so PADDING_AFTER also keeps it whole (R-REC-8).
PADDING_BEFORE = 1.0
PADDING_AFTER = 2.0


class ActivityEvent(TypedDict):
    id: str
    event: str
    time: float


@contextmanager
def log_activity(session: Path) -> Generator[None, None, None]:
    """Record check/capture spans without retaining conditions, text, or errors."""
    identifier = uuid.uuid4().hex

    def append(event: str) -> None:
        with (session / "activity.jsonl").open("a", encoding="utf-8") as output:
            output.write(
                json.dumps({"id": identifier, "event": event, "time": time.monotonic()})
                + "\n"
            )

    append("start")
    try:
        yield
    finally:
        append("end")


def activity_events(log: Path) -> list[ActivityEvent]:
    """Read a JSONL log, leaving a malformed complete line to raise."""
    # A recording may stop while the input worker appends a record. Ignore only
    # the unfinished trailing line; a complete start without an end stays active.
    # Split on the one line end writers use: splitlines would also break a line
    # at a stray carriage return and hide its first half as unfinished.
    *complete, _unfinished = log.read_bytes().split(b"\n")
    return [json.loads(line) for line in complete]


def retained_frames(
    logs: tuple[Path, ...],
    *,
    origin: float,
    stopped: float,
    duration: float,
) -> list[tuple[int, int]]:
    """Return merged half-open frame ranges, rounding cuts outward at 30 fps."""
    frame_count = max(1, round(duration * 30))
    ranges: list[tuple[int, int]] = []

    def retain(start: float, end: float) -> None:
        first = max(0, math.floor(max(0.0, start) * 30))
        last = min(frame_count, math.ceil(min(duration, end) * 30))
        if first < last:
            ranges.append((first, last))

    retain(0, PADDING_BEFORE)
    retain(duration - PADDING_AFTER, duration)
    for log in logs:
        events = activity_events(log)
        ends = {
            event["id"]: event["time"] for event in events if event["event"] == "end"
        }
        for event in events:
            if event["event"] != "start":
                continue
            start = event["time"]
            end = ends.get(event["id"], stopped)
            if start >= stopped or end < origin:
                continue
            retain(start - origin - PADDING_BEFORE, end - origin + PADDING_AFTER)

    merged: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    return merged


def compact_filter(ranges: list[tuple[int, int]]) -> str:
    assert ranges
    terms = [f"between(n,{first},{last - 1})" for first, last in ranges]
    # FFmpeg limits expression recursion. Balance the sum so many disjoint cuts
    # add logarithmic depth instead of one level per retained range.
    while len(terms) > 1:
        terms = [
            f"({'+'.join(terms[index:index + 2])})" for index in range(0, len(terms), 2)
        ]
    selected = terms[0]
    return f"fps=30,select='{selected}',setpts=N/(30*TB)"


def recording_duration(path: Path) -> float:
    """Use the encoded timeline so stop/encoder latency cannot remove the tail."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError(
            f"Could not inspect recording {path}: {result.stderr.strip()}"
        )
    try:
        duration = float(result.stdout)
    except ValueError:
        raise RuntimeError(
            f"Recording {path} has no readable video duration."
        ) from None
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Recording {path} has no positive, finite video duration.")
    return duration

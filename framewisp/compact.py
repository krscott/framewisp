"""Compaction: command-based cuts to a clip on the original monotonic timeline."""

import fcntl
import json
import math
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict, cast

from framewisp.errors import SessionError
from framewisp.processes import run_managed

# Seconds kept around each activity span and at each end of a clip. A click ping
# starts inside its input's span, so PADDING_AFTER also keeps it whole (R-REC-8).
PADDING_BEFORE = 1.0
PADDING_AFTER = 2.0


class ActivityEvent(TypedDict):
    id: str
    event: str
    time: float


@contextmanager
def log_activity(session_dir: Path) -> Generator[None, None, None]:
    """Record check and screenshot spans without retaining conditions, text, or errors."""
    identifier = uuid.uuid4().hex

    def append(event: str) -> None:
        with (session_dir / "activity.jsonl").open("a", encoding="utf-8") as output:
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
    with log.open("rb") as source:
        # Tail repair truncates and replaces bytes. Keep one snapshot across reads.
        fcntl.flock(source, fcntl.LOCK_SH)
        snapshot = source.read()
    *complete, _unfinished = snapshot.split(b"\n")
    events: list[ActivityEvent] = []
    for line_number, line in enumerate(complete, start=1):
        try:
            event = json.loads(line)
        except ValueError as error:
            detail = (
                f"{error.msg} (column {error.colno})"
                if isinstance(error, json.JSONDecodeError)
                else str(error)
            )
            raise ValueError(
                f"Malformed line {line_number} in {log}: {detail}"
            ) from None
        if not isinstance(event, dict):
            raise ValueError(
                f"Malformed line {line_number} in {log}: expected a JSON object"
            )
        events.append(cast(ActivityEvent, event))
    return events


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
        first = math.floor(max(0.0, start) * 30)
        last = min(frame_count, math.ceil(min(duration, end) * 30))
        if first < last:
            ranges.append((first, last))

    retain(0, PADDING_BEFORE)
    retain(duration - PADDING_AFTER, duration)
    for log in logs:
        events = activity_events(log)
        try:
            ends = {
                event["id"]: event["time"]
                for event in events
                if event["event"] == "end"
            }
            for event in events:
                if event["event"] != "start":
                    continue
                start = event["time"]
                end = ends.get(event["id"], stopped)
                if start >= stopped or end < origin:
                    continue
                retain(start - origin - PADDING_BEFORE, end - origin + PADDING_AFTER)
        except (KeyError, TypeError) as error:
            raise ValueError(f"Invalid event in {log}: {error}") from None

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


def ffprobe(path: Path, *arguments: str, env: dict[str, str]) -> str:
    """Return ffprobe's output for `path`, run with `run_managed`."""
    try:
        # In a session, `recover` removes what a killed runner leaves.
        with tempfile.TemporaryDirectory(
            prefix="framewisp-ffprobe-", dir=env.get("FRAMEWISP_RUNTIME_DIRECTORY")
        ) as directory:
            work = Path(directory)
            output = work / "output"
            result = run_managed(
                ["ffprobe", "-v", "error", *arguments, "-o", str(output), str(path)],
                log=work / "ffprobe.log",
                env=env,
                timeout=10,
            )
            if result:
                problem = (work / "ffprobe.log").read_text(errors="replace").strip()
            else:
                return output.read_text(errors="replace")
    except OSError as error:
        problem = str(error)
    raise SessionError(f"Could not inspect clip {path}: {problem}")


def clip_duration(path: Path, *, env: dict[str, str]) -> float:
    """Use the encoded timeline so stop/encoder latency cannot remove the tail."""
    output = ffprobe(
        path,
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        env=env,
    )
    try:
        duration = float(output)
    except ValueError:
        raise RuntimeError(f"Clip {path} has no readable video duration.") from None
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"Clip {path} has no positive, finite video duration.")
    return duration

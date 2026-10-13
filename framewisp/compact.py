"""Command-based recording cuts on the original monotonic timeline."""

import json
import math
import subprocess
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast


class ActivityEvent(TypedDict):
    id: str
    event: str
    time: float


@dataclass(frozen=True)
class CompactSettings:
    before: float = 1.0
    after: float = 2.0

    def __post_init__(self) -> None:
        for value in (self.before, self.after):
            if type(value) not in {int, float}:
                raise ValueError("compact padding must be finite and nonnegative.")
            try:
                valid = math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("compact padding must be finite and nonnegative.")

    @staticmethod
    def parse(raw: object) -> "CompactSettings | None":
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("compact must be an object with before and after seconds.")
        settings = cast(dict[str, object], raw)
        if settings.keys() != {"before", "after"}:
            raise ValueError("compact requires only before and after seconds.")
        return CompactSettings(
            before=cast(float, settings["before"]),
            after=cast(float, settings["after"]),
        )

    def parameters(self) -> dict[str, float]:
        return {"before": self.before, "after": self.after}


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
    # A recording may stop while the input worker appends a record. Ignore only
    # the unfinished trailing line; a complete start without an end stays active.
    return [
        json.loads(line)
        for line in log.read_bytes().splitlines(keepends=True)
        if line.endswith(b"\n")
    ]


def retained_frames(
    logs: tuple[Path, ...],
    *,
    origin: float,
    stopped: float,
    duration: float,
    settings: CompactSettings,
) -> list[tuple[int, int]]:
    """Return merged half-open frame ranges, rounding cuts outward at 30 fps."""
    frame_count = max(1, round(duration * 30))
    ranges = [(0, 1), (frame_count - 1, frame_count)]

    def retain(start: float, end: float) -> None:
        first = max(0, math.floor(max(0.0, start) * 30))
        last = min(frame_count, math.ceil(min(duration, end) * 30))
        if first < last:
            ranges.append((first, last))

    retain(0, min(duration, settings.before))
    retain(max(0.0, duration - settings.after), duration)
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
            retain(start - origin - settings.before, end - origin + settings.after)

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

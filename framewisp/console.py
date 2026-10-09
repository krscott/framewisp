"""Timestamp app output without changing its bytes or terminal behavior."""

import codecs
import json
import math
import os
import select
import shutil
import time
import unicodedata
from collections import deque
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import BinaryIO, Literal, TextIO

PANEL_WIDTH = 640


@dataclass
class ConsoleCapture:
    session: Path
    lock: Lock = field(default_factory=Lock)
    task: Future[None] | None = field(default=None, init=False)

    def check(self) -> None:
        if self.task is not None and self.task.done():
            self.task.result()

    def wait(self) -> None:
        if self.task is not None:
            try:
                self.task.result(timeout=1)
            except FutureTimeout:
                raise RuntimeError(
                    "App output did not close within 1s. Inspect app descendants; "
                    f"log: {self.session / 'app.log'}"
                ) from None

    def snapshot(self, destination: Path) -> None:
        with self.lock:
            shutil.copyfile(self.session / "console.jsonl", destination)

    def drain(self, reader: int, stopped: Event, raw: BinaryIO, events: TextIO) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def append(data: bytes, *, final: bool = False) -> None:
            received = time.monotonic()
            text = decoder.decode(data, final=final)
            with self.lock:
                raw.write(data)
                raw.flush()
                if text:
                    events.write(json.dumps({"time": received, "text": text}) + "\n")
                    events.flush()

        while not stopped.is_set():
            if not select.select([reader], [], [], 0.1)[0]:
                continue
            data = os.read(reader, 65536)
            if not data:
                break
            append(data)
        append(b"", final=True)


@contextmanager
def app_output(capture: ConsoleCapture) -> Generator[BinaryIO, None, None]:
    reader, writer = os.pipe()
    stopped = Event()
    with (
        os.fdopen(reader, "rb") as source,
        os.fdopen(writer, "wb") as sink,
        (capture.session / "app.log").open("wb") as raw,
        (capture.session / "console.jsonl").open("w", encoding="utf-8") as events,
        ThreadPoolExecutor(max_workers=1) as worker,
    ):
        task = worker.submit(capture.drain, source.fileno(), stopped, raw, events)
        capture.task = task
        failed = False
        try:
            yield sink
        except BaseException:
            failed = True
            raise
        finally:
            sink.close()
            try:
                task.result(timeout=1)
            except FutureTimeout:
                stopped.set()
                task.result()
                if not failed:
                    raise RuntimeError(
                        "App output did not close within 1s. Inspect app descendants; "
                        f"log: {capture.session / 'app.log'}"
                    ) from None


@dataclass
class ConsoleText:
    rows: int
    columns: int = 41
    lines: deque[tuple[float, str]] = field(
        default_factory=lambda: deque[tuple[float, str]]()
    )
    current: str = ""
    timestamp: float = 0
    replacing: bool = False
    escape: Literal["", "escape", "csi", "osc", "osc-escape"] = ""

    def newline(self) -> None:
        self.lines.append((self.timestamp, self.current))
        while len(self.lines) > self.rows:
            self.lines.popleft()
        self.current = ""
        self.replacing = False

    def feed(self, text: str, timestamp: float) -> None:
        for char in text:
            if self.escape:
                if self.escape == "escape":
                    self.escape = "csi" if char == "[" else "osc" if char == "]" else ""
                elif self.escape == "csi":
                    if "@" <= char <= "~":
                        self.escape = ""
                elif self.escape == "osc":
                    if char == "\x07":
                        self.escape = ""
                    elif char == "\x1b":
                        self.escape = "osc-escape"
                else:
                    self.escape = "" if char == "\\" else "osc"
                continue
            if char == "\x1b":
                self.escape = "escape"
            elif char == "\r":
                self.replacing = True
            elif char == "\n":
                self.newline()
            elif char == "\b":
                self.current = self.current[:-1]
            elif char == "\t":
                self.feed(" " * (8 - len(self.current) % 8), timestamp)
            elif not unicodedata.category(char).startswith("C"):
                if self.replacing:
                    self.current = ""
                    self.replacing = False
                width = sum(cell_width(c) for c in self.current)
                if (
                    width + cell_width(char) > self.columns
                    or len(self.current) >= self.columns * 4
                ):
                    self.newline()
                if not self.current:
                    self.timestamp = timestamp
                self.current += char

    def screen(self, origin: float) -> str:
        lines = list(self.lines)
        if self.current:
            lines.append((self.timestamp, self.current))
        return "\n".join(
            f"{timestamp - origin:7.2f} | {text}"
            for timestamp, text in lines[-self.rows :]
        )


def cell_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


@dataclass(frozen=True)
class ConsoleFrame:
    start: float
    end: float
    text: str


def console_frames(
    log: Path, *, origin: float, stopped: float, height: int
) -> Generator[ConsoleFrame, None, None]:
    """Keep pre-clip context and coalesce bursts to at most one update per frame."""
    state = ConsoleText(rows=max(1, (height - 64) // 24))
    start = 0.0
    screen = ""
    with log.open(encoding="utf-8") as source:
        for line in source:
            event = json.loads(line)
            timestamp = float(event["time"])
            if timestamp >= stopped:
                break
            effective = max(0.0, math.ceil((timestamp - origin) * 30) / 30)
            if effective > start:
                yield ConsoleFrame(start, min(effective, stopped - origin), screen)
                start = effective
            state.feed(event["text"], timestamp)
            screen = state.screen(origin)
    if start < stopped - origin:
        yield ConsoleFrame(start, stopped - origin, screen)

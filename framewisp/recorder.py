"""Draining the screen recorder's output into its log during recording."""

import os
import re
import select
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import BinaryIO


def filter_recorder_output(source: Iterable[bytes], destination: BinaryIO) -> None:
    """Keep diagnostics and the first frame's clock, discarding protocol chatter."""
    kept_origin = False
    protocol = re.compile(
        rb"^\[[\d:. ]+\]\s+(?:\{[^}]*\}\s+)?(?:(?:->|discarded)\s+)?"
        rb"\w+[#@]\d+\.\w+\(.*\)\s*$"
    )
    for line in source:
        origin = re.search(rb"zwlr_screencopy_frame_v1[#@]\d+\.ready\(", line)
        if origin and not kept_origin:
            kept_origin = True
        elif protocol.match(line):
            continue
        destination.write(line)
        destination.flush()


def recorder_lines(reader: int, stopped: Event) -> Generator[bytes]:
    """Read complete lines without blocking shutdown on an inherited pipe writer."""
    pending: bytes = b""
    while not stopped.is_set():
        if not select.select([reader], [], [], 0.1)[0]:
            continue
        chunk = os.read(reader, 65536)
        if not chunk:
            break
        pending = b"".join((pending, chunk))
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line + b"\n"
    if pending:
        yield pending


@contextmanager
def recorder_output(log: Path) -> Generator[BinaryIO, None, None]:
    """Drain recorder output continuously so protocol tracing cannot fill the log."""
    reader, writer = os.pipe()
    stopped = Event()
    with (
        os.fdopen(reader, "rb") as source,
        os.fdopen(writer, "wb") as sink,
        log.open("wb") as destination,
        ThreadPoolExecutor(max_workers=1) as worker,
    ):
        task = worker.submit(
            filter_recorder_output,
            recorder_lines(source.fileno(), stopped),
            destination,
        )
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
                        f"Recorder output did not close within 1s; an inherited log pipe "
                        f"is still open. Inspect recorder descendants; log: {log}"
                    ) from None

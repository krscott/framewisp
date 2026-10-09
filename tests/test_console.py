import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from framewisp.console import ConsoleCapture, ConsoleText, app_output, console_frames


@pytest.fixture
def capture(tmp_path: Path) -> ConsoleCapture:
    return ConsoleCapture(tmp_path)


def test_capture_preserves_bytes_and_partial_unicode(capture: ConsoleCapture) -> None:
    started = time.monotonic()
    pieces = [b"before\n\x1b[31m", b"caf\xc3", b"\xa9\r", b"x" * 200000, b"\xff\xe2"]
    with app_output(capture) as sink:
        for piece in pieces:
            sink.write(piece)
            sink.flush()
        # Output without a newline must reach the log while the app is running.
        deadline = time.monotonic() + 5
        while (capture.session / "app.log").stat().st_size < sum(map(len, pieces)):
            capture.check()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        capture.snapshot(capture.session / "snapshot.jsonl")
        assert (capture.session / "snapshot.jsonl").read_text().endswith("\n")
    assert (capture.session / "app.log").read_bytes() == b"".join(pieces)
    events = [
        json.loads(line)
        for line in (capture.session / "console.jsonl").read_text().splitlines()
    ]
    assert "".join(event["text"] for event in events) == b"".join(pieces).decode(
        "utf-8", errors="replace"
    )
    assert all(started <= event["time"] <= time.monotonic() for event in events)
    assert [event["time"] for event in events] == sorted(
        event["time"] for event in events
    )


@pytest.fixture
def inherited_writer() -> Iterator[list[int]]:
    descriptors: list[int] = []
    yield descriptors
    for descriptor in descriptors:
        os.close(descriptor)


def test_capture_shutdown_is_bounded(
    capture: ConsoleCapture, inherited_writer: list[int]
) -> None:
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="App output did not close"):
        with app_output(capture) as sink:
            inherited_writer.append(os.dup(sink.fileno()))
            sink.write(b"no newline")
            sink.flush()
    assert time.monotonic() - started < 2
    assert (capture.session / "app.log").read_bytes() == b"no newline"


def test_text_handles_split_ansi_progress_and_unicode() -> None:
    state = ConsoleText(rows=3, columns=12)
    for text in [
        "\x1b[3",
        "1mred\x1b[0m\n",
        "\x1b]0;hidden",
        "\x1b",
        "\\",
        "10%\r",
        "100%\r\n",
        "日本語 cafe\u0301",
    ]:
        state.feed(text, 2)
    assert state.screen(1).splitlines() == [
        "   1.00 | red",
        "   1.00 | 100%",
        "   1.00 | 日本語 cafe\u0301",
    ]
    assert "hidden" not in state.screen(1)


def test_text_bounds_long_lines_and_scrollback() -> None:
    state = ConsoleText(rows=2, columns=4)
    state.feed("abcdefghijk", 1)
    assert state.screen(0).splitlines() == ["   1.00 | efgh", "   1.00 | ijk"]
    state.feed("\u0301" * 10000, 2)
    assert len(state.current) <= 16
    assert len(state.lines) <= 2


def test_clip_has_context_coalesces_bursts_and_excludes_later_output(
    tmp_path: Path,
) -> None:
    log = tmp_path / "console.jsonl"
    log.write_text(
        "".join(
            json.dumps({"time": timestamp, "text": text}) + "\n"
            for timestamp, text in [
                (8, "setup\n"),
                (10.01, "first"),
                (10.02, " update\n"),
                (11, "second\n"),
                (12, "excluded"),
            ]
        )
    )
    frames = list(console_frames(log, origin=10, stopped=12, height=720))
    assert len(frames) == 3
    assert frames[0].start == 0
    assert frames[0].text == "  -2.00 | setup"
    assert frames[1].start == pytest.approx(1 / 30)
    assert "first update" in frames[1].text
    assert "second" not in frames[1].text
    assert frames[2].start == 1
    assert frames[2].end == 2
    assert "second" in frames[2].text
    assert all("excluded" not in frame.text for frame in frames)

"""The input log: a session's record of the actions it sent."""

import fcntl
import json
import os
import time
import unicodedata
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Literal, cast

from framewisp.compact import ActivityEvent
from framewisp.errors import SessionError
from framewisp.keys import parse_chord

KEY_ACTIONS = frozenset({"key", "type"})


class CaptionCapture:
    """Coordinate clip boundaries with the worker writing input starts."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.texts: dict[str, str] | None = None

    @contextmanager
    def clip(self, *, enabled: bool) -> Generator[dict[str, str], None, None]:
        texts: dict[str, str] = {}
        if enabled:
            with self.lock:
                self.texts = texts
        try:
            yield texts
        finally:
            if enabled:
                self.stop()

    def stop(self) -> float:
        with self.lock:
            self.texts = None
            return time.monotonic()


def retained_parameters(
    action: str, parameters: dict[str, object]
) -> dict[str, object]:
    """Keep shortcuts and action metadata without retaining literal keystrokes."""
    p = parameters.copy()
    if action == "type":
        p.pop("text", None)
    elif action == "key":
        chord = cast(str, p.pop("chord"))
        if p.get("private") is True:
            return p
        parsed = parse_chord(chord)
        assert parsed is not None
        modifiers, key = parsed
        # Ctrl/Alt chords describe shortcuts. Shift alone can still type text.
        if {"ctrl", "alt"}.intersection(modifiers) or not (
            len(key) == 1 or key == "space"
        ):
            p["chord"] = chord
        else:
            p["modifier"] = modifiers
    return p


def input_error(command: str, error: BaseException) -> str:
    """Backend errors can include text, keysyms, or serialized subprocess arguments."""
    if command not in KEY_ACTIONS:
        return str(error)
    if isinstance(error, InterruptedError):
        return "Input cancelled; caller disconnected or session stopped."
    if isinstance(error, SessionError) and error.input_message is not None:
        return f"{type(error).__name__}: {error.input_message}"
    details = "input details omitted"
    returncode = getattr(error, "returncode", None)
    if isinstance(returncode, int):
        details += f"; exit {returncode}"
    return f"{type(error).__name__}: {details}"


class InputEvent(ActivityEvent):
    action: str
    parameters: dict[str, object]
    returncode: int | None
    error: str | None


@dataclass
class InputResult:
    press: Callable[[int, int], None]
    release: Callable[[int, int], None]
    returncode: int | None = None
    error: str | None = None


def _discard_unfinished_tail(output: BinaryIO) -> None:
    """A newline commits a record, even when its JSON is malformed."""
    end = output.seek(0, os.SEEK_END)
    if not end:
        return
    output.seek(end - 1)
    if output.read(1) == b"\n":
        return
    position = end - 1
    while position:
        start = max(0, position - 8192)
        output.seek(start)
        newline = output.read(position - start).rfind(b"\n")
        if newline >= 0:
            output.truncate(start + newline + 1)
            return
        position = start
    output.truncate(0)


@contextmanager
def log_input(
    session_dir: Path,
    action: str,
    parameters: dict[str, object],
    *,
    caption_capture: CaptionCapture | None = None,
) -> Generator[InputResult, None, None]:
    """Record command start/end and button presses and drag releases.

    These are not app acknowledgements.
    """
    identifier = uuid.uuid4().hex
    retained = retained_parameters(action, parameters)
    captures_literal = (
        action in KEY_ACTIONS
        and retained.get("private") is not True
        and "chord" not in retained
    )
    capture = caption_capture if captures_literal else None

    def append(
        event: Literal["start", "press", "release", "end"], **point: int
    ) -> None:
        timestamp = time.monotonic()
        with (session_dir / "inputs.jsonl").open("a+b") as output:
            # Attached CLI clients can append concurrently. Closing flushes the
            # buffered record before releasing this descriptor's file lock.
            fcntl.flock(output, fcntl.LOCK_EX)
            _discard_unfinished_tail(output)
            if event == "start" and capture is not None:
                # The renderer's shared flock waits for this start to reach
                # the log. The clip lock never spans file operations.
                with capture.lock:
                    timestamp = time.monotonic()
                    if capture.texts is not None:
                        capture.texts[identifier] = caption_text(action, parameters)
            entry = {
                "id": identifier,
                "event": event,
                "time": timestamp,
                "action": action,
                "parameters": retained,
                "returncode": result.returncode,
                "error": result.error,
                **point,
            }
            record = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
            output.write(record)

    result = InputResult(
        press=lambda x, y: append("press", x=x, y=y),
        release=lambda x, y: append("release", x=x, y=y),
    )
    append("start")
    try:
        yield result
    except BaseException as error:
        # Preserve the exception and traceback while recording why the command ended.
        returncode = getattr(error, "returncode", None)
        if isinstance(returncode, int):
            result.returncode = returncode
        detail = input_error(action, error)
        result.error = (
            f"{type(error).__name__}: {detail}" if action not in KEY_ACTIONS else detail
        )
        raise
    finally:
        append("end")


def caption_text(action: str, p: dict[str, object]) -> str:
    """The caption for an action.

    The input log omits literal text, so `log_input` takes its caption while
    the text is still at hand.
    """
    modifiers = cast(list[str], p.get("modifier", []))
    prefix = "".join(f"{str(modifier).title()}+" for modifier in modifiers)
    if action == "type":
        text = f"Type: {p['text']}"
    elif action == "key":
        text = str(p["chord"])
    elif action == "drag":
        text = f"{prefix}{p['button']} drag ({p['x1']}, {p['y1']}) to ({p['x2']}, {p['y2']})"
    elif action == "click":
        count = "Double " if p["count"] == 2 else ""
        text = f"{prefix}{count}{p['button']} click ({p['x']}, {p['y']})"
    elif action == "scroll":
        text = f"Scroll {p['direction']} {p['ticks']} at ({p['x']}, {p['y']})"
    else:
        text = f"Move ({p['x']}, {p['y']})"
    # Reserve enough width for CJK glyphs as well as Latin text.
    width = 0
    for index, character in enumerate(text):
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if width > 76:
            return text[:index] + "..."
    return text

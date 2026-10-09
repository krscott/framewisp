"""Input action records and text rendered into completed recordings."""

import json
import os
import re
import select
import subprocess
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import BinaryIO, Literal, TypedDict, cast

from framewisp.console import PANEL_WIDTH, ConsoleCapture, console_frames
from framewisp.errors import SessionError


def retained_parameters(
    action: str, parameters: dict[str, object], *, retain_input_content: bool
) -> dict[str, object]:
    """Keep shortcuts and action metadata without retaining literal keystrokes."""
    p = parameters.copy()
    if retain_input_content:
        return p
    if action == "type":
        p.pop("text", None)
    elif action == "key":
        chord = cast(str, p.pop("chord"))
        *modifiers, key = chord.lower().split("+")
        # Ctrl/Alt chords describe shortcuts. Shift alone can still type text.
        if {"ctrl", "alt"}.intersection(modifiers) or key not in {
            *"abcdefghijklmnopqrstuvwxyz0123456789",
            "space",
        }:
            p["chord"] = chord
        else:
            p["modifier"] = modifiers
    return p


def input_error(
    action: str, error: BaseException, *, retain_input_content: bool
) -> str:
    """Backend errors can include text, keysyms, or serialized subprocess arguments."""
    if retain_input_content or action not in {"type", "key"}:
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


class InputEvent(TypedDict):
    id: str
    event: str
    time: float
    action: str
    parameters: dict[str, object]
    returncode: int | None
    error: str | None


@dataclass
class InputResult:
    returncode: int | None = None
    error: str | None = None


@contextmanager
def log_input(
    session: Path,
    action: str,
    parameters: dict[str, object],
    *,
    retain_input_content: bool = False,
) -> Generator[InputResult, None, None]:
    """Record command start/end, including failures; these are not app acknowledgements."""
    identifier = uuid.uuid4().hex
    result = InputResult()
    parameters = retained_parameters(
        action, parameters, retain_input_content=retain_input_content
    )

    def append(event: Literal["start", "end"]) -> None:
        entry = {
            "id": identifier,
            "event": event,
            "time": time.monotonic(),
            "action": action,
            "parameters": parameters,
            **asdict(result),
        }
        with (session / "inputs.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, ensure_ascii=False) + "\n")

    append("start")
    try:
        yield result
    except BaseException as error:
        # Preserve the exception and traceback while recording why the command ended.
        returncode = getattr(error, "returncode", None)
        if isinstance(returncode, int):
            result.returncode = returncode
        detail = input_error(action, error, retain_input_content=retain_input_content)
        result.error = (
            f"{type(error).__name__}: {detail}"
            if retain_input_content or action not in {"type", "key"}
            else detail
        )
        raise
    finally:
        append("end")


def capture_origin(log: Path) -> float:
    """Read the first captured frame's monotonic timestamp on our Sway backend."""
    with log.open() as source:
        for line in source:
            match = re.search(
                r"zwlr_screencopy_frame_v1[#@]\d+\.ready\((\d+), (\d+), (\d+)\)",
                line,
            )
            if match:
                high, low, nanoseconds = map(int, match.groups())
                return (high << 32) + low + nanoseconds / 1_000_000_000
    raise RuntimeError(f"Missing first-frame timestamp. See {log}")


def caption_text(event: InputEvent) -> str:
    p = event["parameters"]
    action = event["action"]
    modifiers = cast(list[str], p.get("modifier", []))
    prefix = "".join(f"{str(modifier).title()}+" for modifier in modifiers)
    if action == "type":
        text = f"Type: {p['text']}" if "text" in p else "Type text"
    elif action == "key":
        text = str(p["chord"]) if "chord" in p else f"{prefix}Key"
    elif action == "drag":
        text = f"{prefix}{p['button']} drag ({p['x1']}, {p['y1']}) to ({p['x2']}, {p['y2']})"
    elif action == "click":
        count = "Double " if p["count"] == 2 else ""
        text = f"{prefix}{count}{p['button']} click ({p['x']}, {p['y']})"
    elif action == "scroll":
        text = f"Scroll {p['direction']} {p['steps']} at ({p['x']}, {p['y']})"
    else:
        text = f"Move ({p['x']}, {p['y']})"
    # Reserve enough width for CJK glyphs as well as Latin text.
    width = 0
    for index, character in enumerate(text):
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if width > 76:
            return text[:index] + "..."
    return text


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str


def captions_for_clip(log: Path, *, origin: float, stopped: float) -> list[Caption]:
    events: list[InputEvent] = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    starts = sorted(
        (
            event
            for event in events
            if event["event"] == "start" and origin <= event["time"] < stopped
        ),
        key=lambda event: event["time"],
    )
    ends = {event["id"]: event for event in events if event["event"] == "end"}
    captions: list[Caption] = []
    for index, event in enumerate(starts):
        end = ends.get(event["id"])
        finished = end["time"] if end is not None else stopped
        limit = starts[index + 1]["time"] if index + 1 < len(starts) else stopped
        text = caption_text(event)
        if end is not None and (end["returncode"] != 0 or end["error"] is not None):
            text += " (failed)"
        captions.append(
            Caption(
                event["time"] - origin,
                min(max(finished, event["time"] + 0.8), limit, stopped) - origin,
                text,
            )
        )
    return captions


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    minutes, remainder = divmod(centiseconds, 6000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{remainder // 100:02}.{remainder % 100:02}"


def subtitle_script(
    captions: list[Caption], *, size: tuple[int, int] = (1280, 720), panel: bool = False
) -> str:
    width, height = size
    scale = height / 720
    extra = PANEL_WIDTH if panel else 0
    script = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width + extra}
PlayResY: {height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Default,Noto Sans,{24 * scale},&H00FFFFFF,&H80000000,3,{8 * scale},0,2,{24 * width / 1280},{extra + 24 * width / 1280},{24 * scale}
Style: Console,DejaVu Sans Mono,18,&H00EEEEEE,&H00000000,1,0,0,7,0,0,0

[Events]
Format: Layer, Start, End, Style, Text
"""
    for caption in captions:
        # ASS interprets braces and backslashes as formatting. Use visible fullwidth
        # equivalents in captions; opted-in input logs retain the original text.
        text = caption.text.translate(str.maketrans("\\{}", "＼｛｝"))
        script += f"Dialogue: 0,{ass_time(caption.start)},{ass_time(caption.end)},Default,{text}\n"
    return script


def render_recording(
    destination: Path,
    *,
    input_log: Path,
    origin: float,
    stopped: float,
    log: Path,
    captions: bool = True,
    console: ConsoleCapture | None = None,
    size: tuple[int, int] = (1280, 720),
) -> None:
    entries = (
        captions_for_clip(input_log, origin=origin, stopped=stopped) if captions else []
    )
    if not entries and console is None:
        return
    with tempfile.TemporaryDirectory(
        prefix=".framewisp-captions-", dir=destination.parent
    ) as directory:
        work = Path(directory)
        with (work / "captions.ass").open("w", encoding="utf-8") as script:
            script.write(subtitle_script(entries, size=size, panel=console is not None))
            if console is not None:
                console.snapshot(work / "console.jsonl")
                x, height = size
                position = (
                    f"{{\\pos({x + 16},16)\\clip({x},0,{x + PANEL_WIDTH},{height})}}"
                )
                script.write(
                    f"Dialogue: 0,0:00:00.00,{ass_time(stopped - origin)},Console,"
                    f"{position}Console (stdout + stderr)\n"
                )
                position = (
                    f"{{\\pos({x + 16},48)\\clip({x},0,{x + PANEL_WIDTH},{height})}}"
                )
                for frame in console_frames(
                    work / "console.jsonl",
                    origin=origin,
                    stopped=stopped,
                    height=height,
                ):
                    text = frame.text.translate(
                        str.maketrans("\\{}", "＼｛｝")
                    ).replace("\n", r"\N")
                    script.write(
                        f"Dialogue: 0,{ass_time(frame.start)},{ass_time(frame.end)},Console,{position}{text}\n"
                    )
        filters: list[str] = []
        if console is not None:
            filters.append(f"pad=iw+{PANEL_WIDTH}:ih:0:0:color=0x161b22")
        filters.extend(["ass=captions.ass", "scale=out_range=full"])
        with log.open("w") as output:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "warning",
                    "-i",
                    str(destination),
                    "-vf",
                    ",".join(filters),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-color_range",
                    "pc",
                    "-an",
                    "-movflags",
                    "+faststart",
                    "captioned.mp4",
                ],
                env=os.environ
                | {"FONTCONFIG_FILE": os.environ["FRAMEWISP_FONTCONFIG_FILE"]},
                cwd=work,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if result.returncode:
            raise RuntimeError(
                f"Recording rendering failed; raw video remains at {destination}. See {log}"
            )
        (work / "captioned.mp4").replace(destination)

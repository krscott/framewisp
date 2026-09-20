"""Input action records and captions rendered into completed recordings."""

import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Literal, TypedDict, cast


def filter_recorder_output(source: BinaryIO, destination: BinaryIO) -> None:
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


@contextmanager
def recorder_output(log: Path) -> Generator[BinaryIO, None, None]:
    """Drain recorder output continuously so protocol tracing cannot fill the log."""
    reader, writer = os.pipe()
    with (
        os.fdopen(reader, "rb") as source,
        os.fdopen(writer, "wb") as sink,
        log.open("wb") as destination,
        ThreadPoolExecutor(max_workers=1) as worker,
    ):
        task = worker.submit(filter_recorder_output, source, destination)
        try:
            yield sink
        finally:
            sink.close()
            task.result()


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
    session: Path, action: str, parameters: dict[str, object]
) -> Generator[InputResult, None, None]:
    """Record command start/end, including failures; these are not app acknowledgements."""
    identifier = uuid.uuid4().hex
    result = InputResult()

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
        result.error = f"{type(error).__name__}: {error}"
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
        text = f"Type: {p['text']}"
    elif action == "key":
        text = str(p["chord"])
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


def subtitle_script(captions: list[Caption]) -> str:
    script = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Default,Noto Sans,24,&H00FFFFFF,&H80000000,3,8,0,2,24,24,24

[Events]
Format: Layer, Start, End, Style, Text
"""
    for caption in captions:
        # ASS interprets braces and backslashes as formatting. Use visible fullwidth
        # equivalents in captions; the separate input log retains the original text.
        text = caption.text.translate(str.maketrans("\\{}", "＼｛｝"))
        script += f"Dialogue: 0,{ass_time(caption.start)},{ass_time(caption.end)},Default,{text}\n"
    return script


def render_captions(
    destination: Path, *, input_log: Path, origin: float, stopped: float, log: Path
) -> None:
    captions = captions_for_clip(input_log, origin=origin, stopped=stopped)
    if not captions:
        return
    with tempfile.TemporaryDirectory(
        prefix=".framewisp-captions-", dir=destination.parent
    ) as directory:
        work = Path(directory)
        (work / "captions.ass").write_text(subtitle_script(captions), encoding="utf-8")
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
                    "ass=captions.ass,scale=out_range=full",
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
                f"Caption rendering failed; uncaptioned video remains at {destination}. See {log}"
            )
        (work / "captioned.mp4").replace(destination)

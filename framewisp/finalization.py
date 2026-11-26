"""Finalization's FFmpeg pass: compaction, and the overlays and console panel drawn onto a clip."""

import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Literal, ParamSpec, cast, get_args

from framewisp.compact import (
    activity_events,
    clip_duration,
    compact_filter,
    retained_frames,
)
from framewisp.console import ConsoleCapture, console_frames
from framewisp.environment import configured_fonts, fixed_fonts
from framewisp.input_log import KEY_ACTIONS, InputEvent, caption_text
from framewisp.layout import CONSOLE_FONT_SIZE, ClipLayout
from framewisp.pointer_overlays import (
    POINTER_STYLE,
    ClickPing,
    HoldDisc,
    pointer_overlay_text,
    pointer_overlays_for_clip,
)
from framewisp.processes import run_managed

KeyCaptions = Literal["all", "non-text"]
PointerCaptions = Literal["all", "modifiers"]
KEY_CAPTIONS: tuple[KeyCaptions, ...] = get_args(KeyCaptions)
POINTER_CAPTIONS: tuple[PointerCaptions, ...] = get_args(PointerCaptions)
POINTER_ACTIONS = frozenset({"move", "click", "drag", "scroll"})


@dataclass(frozen=True)
class CaptionOptions:
    pointer: PointerCaptions | None = None
    key: KeyCaptions | None = None

    @property
    def enabled(self) -> bool:
        return self.pointer is not None or self.key is not None


def capture_origin(log: Path) -> float:
    """Read the first frame's monotonic timestamp from the private compositor."""
    # The recorder logs the path it writes, which can be any bytes.
    with log.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            match = re.search(
                r"zwlr_screencopy_frame_v1[#@]\d+\.ready\((\d+), (\d+), (\d+)\)",
                line,
            )
            if match:
                high, low, nanoseconds = map(int, match.groups())
                return (high << 32) + low + nanoseconds / 1_000_000_000
    raise RuntimeError(f"Missing first-frame timestamp. See {log}")


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str


def captions_for_clip(
    log: Path,
    *,
    origin: float,
    stopped: float,
    captions: CaptionOptions,
    literal_captions: Mapping[str, str] | None = None,
) -> list[Caption]:
    """Unselected actions never become captions, so they also never shorten one."""
    literal: Mapping[str, str] = (
        literal_captions if literal_captions is not None else {}
    )

    def selected(event: InputEvent) -> bool:
        action, parameters = event["action"], event["parameters"]
        if action in KEY_ACTIONS:
            return (
                captions.key is not None
                and parameters.get("private") is not True
                and (
                    "chord" in parameters
                    or (captions.key == "all" and event["id"] in literal)
                )
            )
        return action in POINTER_ACTIONS and (
            captions.pointer == "all"
            or (
                captions.pointer == "modifiers"
                and action in {"click", "drag"}
                and bool(parameters.get("modifier"))
            )
        )

    events = cast(list[InputEvent], activity_events(log))
    starts = sorted(
        (
            event
            for event in events
            if event["event"] == "start"
            and origin <= event["time"] < stopped
            and selected(event)
        ),
        key=lambda event: event["time"],
    )
    ends = {event["id"]: event for event in events if event["event"] == "end"}
    entries: list[Caption] = []
    for index, event in enumerate(starts):
        end = ends.get(event["id"])
        finished = end["time"] if end is not None else stopped
        limit = starts[index + 1]["time"] if index + 1 < len(starts) else stopped
        text = literal.get(event["id"]) if captions.key == "all" else None
        if text is None:
            text = caption_text(event["action"], event["parameters"])
        if end is not None and (end["returncode"] != 0 or end["error"] is not None):
            text += " (failed)"
        entries.append(
            Caption(
                event["time"] - origin,
                min(max(finished, event["time"] + 0.8), limit, stopped) - origin,
                text,
            )
        )
    return entries


def ass_text(text: str) -> str:
    """Draw text literally, at its usual width.

    Libass reads `\\{` and `\\}` as plain braces. A word joiner after each
    backslash keeps it from starting `\\N`, `\\n` or `\\h`.
    """
    return text.replace("\\", "\\\u2060").replace("{", r"\{").replace("}", r"\}")


def ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    minutes, remainder = divmod(centiseconds, 6000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{remainder // 100:02}.{remainder % 100:02}"


def subtitle_script(
    captions: list[Caption],
    *,
    pointer_overlays: Iterable[ClickPing | HoldDisc] = (),
    layout: ClipLayout,
) -> str:
    gui = layout.gui
    # Overlays follow the GUI, so a top or bottom console panel doesn't resize them.
    scale = gui.height / 720
    side_margin = 24 * gui.width / 1280
    margin_left = gui.x + side_margin
    margin_right = layout.width - gui.right + side_margin
    margin_bottom = layout.height - gui.bottom + 24 * scale
    script = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {layout.width}
PlayResY: {layout.height}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Default,Noto Sans,{24 * scale},&H00FFFFFF,&H80000000,3,{8 * scale},0,2,{margin_left},{margin_right},{margin_bottom}
Style: Console,DejaVu Sans Mono,{CONSOLE_FONT_SIZE},&H00EEEEEE,&H00000000,1,0,0,7,0,0,0
{POINTER_STYLE}

[Events]
Format: Layer, Start, End, Style, Text
"""
    for caption in captions:
        text = ass_text(caption.text)
        # A caption is one line, which can be wider than a narrow GUI.
        script += f"Dialogue: 0,{ass_time(caption.start)},{ass_time(caption.end)},Default,{{{gui.clip}}}{text}\n"
    for overlay in pointer_overlays:
        script += f"Dialogue: 1,{ass_time(overlay.start)},{ass_time(overlay.end)},Pointer,{pointer_overlay_text(overlay, layout)}\n"
    return script


P = ParamSpec("P")


def keeps_raw_clip(render: Callable[P, None]) -> Callable[P, None]:
    """Report any failure after capture with the clip it left behind."""

    @wraps(render)
    def rendered(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            render(*args, **kwargs)
        except Exception as error:
            raise RuntimeError(
                f"Finalization failed; raw clip remains at {args[0]} without "
                f"overlays, console panel or compaction. See {kwargs['log']} and "
                f"{kwargs['input_log']}. Cause: {error}"
            ) from error

    return rendered


@keeps_raw_clip
def render_clip(
    clip: Path,
    /,
    *,
    input_log: Path,
    origin: float,
    stopped: float,
    log: Path,
    captions: CaptionOptions = CaptionOptions(),
    literal_captions: Mapping[str, str] | None = None,
    click_pings: bool = True,
    console: ConsoleCapture,
    compact: bool = False,
    layout: ClipLayout,
    env: dict[str, str],
) -> None:
    entries = (
        captions_for_clip(
            input_log,
            origin=origin,
            stopped=stopped,
            captions=captions,
            literal_captions=literal_captions,
        )
        if captions.enabled
        else []
    )
    overlays = (
        pointer_overlays_for_clip(input_log, origin=origin, stopped=stopped)
        if click_pings
        else []
    )
    console_panel = layout.console_panel
    if not entries and not overlays and console_panel is None and not compact:
        return
    with tempfile.TemporaryDirectory(
        prefix=".framewisp-finalization-", dir=clip.parent
    ) as directory:
        work = Path(directory)
        with (work / "subtitles.ass").open("w", encoding="utf-8") as script:
            script.write(
                subtitle_script(entries, pointer_overlays=overlays, layout=layout)
            )
            if console_panel is not None:
                console.snapshot(work / "console.jsonl")
                position = f"{{\\pos({console_panel.x + 16},{console_panel.y + 16}){console_panel.clip}}}"
                script.write(
                    f"Dialogue: 0,0:00:00.00,{ass_time(stopped - origin)},Console,"
                    f"{position}Console (stdout + stderr)\n"
                )
                position = f"{{\\pos({console_panel.x + 16},{console_panel.y + 48}){console_panel.clip}}}"
                for frame in console_frames(
                    work / "console.jsonl",
                    origin=origin,
                    stopped=stopped,
                    console_panel=console_panel,
                    cell_scale=layout.cell_scale,
                ):
                    # Libass drops a line's leading spaces, which would shift the
                    # `|` after a short timestamp. Hard spaces keep the columns.
                    text = ass_text(frame.text).replace(" ", r"\h").replace("\n", r"\N")
                    script.write(
                        f"Dialogue: 0,{ass_time(frame.start)},{ass_time(frame.end)},Console,{position}{text}\n"
                    )
        filters: list[str] = []
        if console_panel is not None:
            filters.append(
                f"pad={layout.width}:{layout.height}:{layout.gui.x}:{layout.gui.y}"
                ":color=0x161b22"
            )
        filters.extend(["ass=subtitles.ass", "scale=out_range=full"])
        if compact:
            filters.append(
                compact_filter(
                    retained_frames(
                        (input_log, input_log.parent / "activity.jsonl"),
                        origin=origin,
                        stopped=stopped,
                        duration=clip_duration(clip, env=env),
                    )
                )
            )
        fonts = configured_fonts(env)
        if fonts is None:
            raise RuntimeError("FRAMEWISP_FONTCONFIG_FILE does not name a file.")
        # A long clip can have more cut ranges than fit in an exec argument.
        (work / "filters.txt").write_text(",".join(filters), encoding="utf-8")
        rendered = work / "rendered.mp4"
        result = run_managed(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "warning",
                "-i",
                str(clip),
                "-/filter:v",
                "filters.txt",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-color_range",
                "pc",
                "-an",
                "-movflags",
                "+faststart",
                str(rendered),
            ],
            log=log,
            env=fixed_fonts(env, fonts=fonts),
            cwd=work,
        )
        if result:
            raise RuntimeError(f"FFmpeg exited with status {result}.")
        rendered.replace(clip)

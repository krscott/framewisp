import argparse
import json
import math
import os
import subprocess
import time
from pathlib import Path

from framewisp import inspection_limits as limits
from framewisp.batch import MAX_REQUEST_BYTES, Batch
from framewisp.captions import KEY_CAPTIONS, POINTER_CAPTIONS, CaptionOptions, log_input
from framewisp.compact import PADDING_AFTER, PADDING_BEFORE
from framewisp.connection import (
    COMMAND_PROTOCOL,
    DESKTOP_KINDS,
    TEACHING_COMMANDS,
    DesktopKind,
    request_desktop,
    session_kind,
    teaching_refusal,
)
from framewisp.desktop import stop_sharing
from framewisp.errors import SessionError, recorded_app_exit
from framewisp.keys import CLICK_BUTTONS, MODIFIERS, SCROLL_BUTTONS
from framewisp.layout import (
    CONSOLE_POSITIONS,
    DEFAULT_CELL_SCALE,
    DEFAULT_CONSOLE_POSITION,
    DEFAULT_CONSOLE_SIZE,
    ConsolePanel,
    video_layout,
)
from framewisp.lib import (
    key_commands,
    run_session,
    screenshot,
    session_command,
)
from framewisp.ownership import recover_session
from framewisp.pointer import DEFAULT_MOVE_DURATION
from framewisp.sessions import resolve_session_dir


def seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError(
            "must be a finite, nonnegative number of seconds"
        )
    return result


def positive_integer(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a headless Wayland or X11 app, or attach to your desktop."
    )
    parser.add_argument(
        "--agent-skill",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the bundled agent skill Markdown and exit (use alone)",
    )
    parser.add_argument(
        "--stop-sharing",
        action="store_true",
        help="emergency stop for whichever session is sharing the desktop",
    )
    parser.add_argument(
        "session",
        nargs="?",
        metavar="SESSION",
        help="project-scoped session name (e.g. browser), or the path of a session directory (e.g. ./browser or /tmp/browser)",
    )
    commands = parser.add_subparsers(dest="command")

    commands.add_parser(
        "status", help="report live headless app and recording state as JSON"
    )
    commands.add_parser(
        "recover", help="recover an abandoned headless session, preserving logs"
    )
    commands.add_parser("stop", help="stop a headless session and wait for cleanup")

    inspect = commands.add_parser(
        "inspect", help="query accessible UI objects in a headless session"
    )
    inspect.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="print a structured observation",
    )
    inspect.add_argument("--role", help="case-insensitive exact AT-SPI role")
    inspect.add_argument("--name", help="case-insensitive accessible name substring")
    inspect.add_argument("--text", help="case-insensitive accessible text substring")
    inspect.add_argument(
        "--max-depth",
        type=int,
        default=limits.DEFAULT_MAX_DEPTH,
        help=f"tree depth, 1-{limits.MAX_DEPTH} (default: {limits.DEFAULT_MAX_DEPTH})",
    )
    inspect.add_argument(
        "--limit",
        type=int,
        default=limits.DEFAULT_MATCH_LIMIT,
        help=f"result count, 1-{limits.MAX_MATCH_LIMIT} (default: {limits.DEFAULT_MATCH_LIMIT})",
    )
    inspect.add_argument(
        "--max-nodes",
        type=int,
        default=limits.DEFAULT_MAX_NODES,
        help=f"visited nodes, 1-{limits.MAX_NODES} (default: {limits.DEFAULT_MAX_NODES})",
    )
    inspect.add_argument(
        "--timeout",
        type=seconds,
        default=limits.DEFAULT_INSPECT_TIMEOUT,
        help=f"query budget, >0 to {limits.MAX_TIMEOUT} seconds (default: {limits.DEFAULT_INSPECT_TIMEOUT:g})",
    )

    batch = commands.add_parser(
        "batch",
        help="run ordered headless steps and an optional screenshot from JSON",
        description="Validate all steps, execute without interleaving, and print JSON results. "
        "Stop at the first failure; completed steps cannot be rolled back. "
        f"The checks baseline/wait/assert accept limits: {{max_nodes: 1..{limits.MAX_NODES} (default {limits.DEFAULT_MAX_NODES}), "
        f"max_depth: 1..{limits.MAX_DEPTH} (default {limits.DEFAULT_MAX_DEPTH}), timeout: >0..{limits.MAX_TIMEOUT} seconds (default {limits.DEFAULT_CHECK_TIMEOUT})}}. "
        f"The required check timeout (>0..{limits.MAX_TIMEOUT} seconds) caps all queries. "
        "Use interval 0 for unpaced typing. A screenshot is not an app acknowledgement.",
    )
    batch.add_argument(
        "--file",
        type=Path,
        required=True,
        help="JSON file with steps and an optional screenshot",
    )

    commands.add_parser(
        "attach", help="share an existing desktop through its permission dialog"
    )
    teach = commands.add_parser(
        "teach",
        help="record a lesson from your desktop without keyboard or pointer access",
    )
    teach.add_argument("directory", type=Path, help="new directory for the lesson")
    teach.add_argument("--goal", required=True, help="what the agent should learn")
    note = commands.add_parser(
        "teach-note", help="add an explanation to an active teaching session"
    )
    note.add_argument("text", help="intent, variable inputs, or an expected result")

    run = commands.add_parser(
        "run", help="run an app until it exits or you interrupt it"
    )
    run.add_argument(
        "--x11", action="store_true", help="run the app on a private Xwayland display"
    )
    run.add_argument(
        "--real-env",
        action="store_true",
        help="give the app the environment framewisp was started with, including "
        "its home, XDG and temp directories, fonts, theme, locale, time zone and "
        "desktop identity, instead of a sanitized one; output can then differ "
        "between hosts",
    )
    run.add_argument(
        "--record", type=Path, metavar="FILE", help="record the session to MP4"
    )
    run.add_argument(
        "--width",
        type=positive_integer,
        default=1280,
        help="display width in pixels (default: 1280)",
    )
    run.add_argument(
        "--height",
        type=positive_integer,
        default=720,
        help="display height in pixels (default: 720)",
    )
    run.add_argument(
        "app_command",
        metavar="APP",
        nargs=argparse.REMAINDER,
        help="-- APP [ARGS...]: the app executable and its arguments",
    )

    record_start = commands.add_parser("record-start", help="start recording a clip")
    record_start.add_argument("path", type=Path, metavar="FILE")
    for recording_parser in (run, record_start):
        recording_parser.add_argument(
            "--compact",
            action="store_true",
            help=f"trim gaps between inputs, keeping {PADDING_BEFORE:g} s before and {PADDING_AFTER:g} s after, preserving batch waits and screenshot delays",
        )
        recording_parser.add_argument(
            "--console",
            action="store_true",
            help="add a panel of timestamped app stdout/stderr to the recorded video",
        )
        recording_parser.add_argument(
            "--console-position",
            choices=CONSOLE_POSITIONS,
            help=f"video edge for the console panel (default: {DEFAULT_CONSOLE_POSITION}; requires --console)",
        )
        recording_parser.add_argument(
            "--console-size",
            type=positive_integer,
            metavar="PIXELS",
            help=f"even panel width for left or right, height for top or bottom; the other dimension follows the GUI (default: {DEFAULT_CONSOLE_SIZE}; requires --console)",
        )
        recording_parser.add_argument(
            "--console-cell-scale",
            type=float,
            metavar="FACTOR",
            help="assume each console text cell is FACTOR times the console font's cell, so fewer rows and columns fit; raise it, e.g. to 1.1, for scripts such as Thai or Devanagari that draw larger (default: 1; requires --console)",
        )
        recording_parser.add_argument(
            "--pointer-captions",
            choices=POINTER_CAPTIONS,
            help="caption all pointer actions or only modified clicks and drags",
        )
        recording_parser.add_argument(
            "--key-captions",
            choices=KEY_CAPTIONS,
            help="caption all keyboard input (including typed text) or only shortcuts and named non-printing keys",
        )
        recording_parser.add_argument(
            "--no-click-pings",
            action="store_true",
            help="record without the red ring at each press and the disc that follows drags",
        )
    commands.add_parser(
        "record-stop", help="finalize the active clip and keep the app running"
    )

    screenshot_parser = commands.add_parser(
        "screenshot", help="save the current display as a PNG"
    )
    screenshot_parser.add_argument("path", type=Path)
    screenshot_parser.add_argument(
        "--region",
        type=int,
        nargs=4,
        metavar=("X", "Y", "WIDTH", "HEIGHT"),
        help="capture a known region in display pixels (headless only); omit for full context",
    )
    screenshot_parser.add_argument(
        "--json",
        action="store_true",
        help="print crop origin, dimensions, display dimensions, path and screenshot time (headless only)",
    )
    screenshot_parser.add_argument(
        "--delay",
        type=seconds,
        default=0.0,
        metavar="SECONDS",
        help="wait this many seconds before capturing (default: 0)",
    )

    move = commands.add_parser("move", help="move the pointer without pressing buttons")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)

    click = commands.add_parser(
        "click", help="send one or two clicks at display coordinates"
    )
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    click.add_argument(
        "--button",
        choices=CLICK_BUTTONS,
        default="left",
        help="mouse button (default: left)",
    )
    click.add_argument(
        "--count",
        type=int,
        choices=[1, 2],
        default=1,
        help="click count, with 0.1 seconds between clicks (default: 1)",
    )

    scroll = commands.add_parser(
        "scroll", help="send wheel ticks at display coordinates"
    )
    scroll.add_argument("x", type=int)
    scroll.add_argument("y", type=int)
    scroll.add_argument("direction", choices=SCROLL_BUTTONS)
    scroll.add_argument(
        "--ticks", type=int, default=1, help="positive wheel tick count (default: 1)"
    )

    drag = commands.add_parser("drag", help="drag with a mouse button")
    drag.add_argument(
        "--button",
        choices=CLICK_BUTTONS,
        default="left",
        help="mouse button (default: left)",
    )
    drag.add_argument("x1", type=int)
    drag.add_argument("y1", type=int)
    drag.add_argument("x2", type=int)
    drag.add_argument("y2", type=int)
    drag.add_argument(
        "--duration",
        type=seconds,
        default=0.4,
        metavar="SECONDS",
        help="time spent dragging (default: 0.4; 0 moves immediately)",
    )

    for pointer in (move, click, scroll, drag):
        pointer.add_argument(
            "--move-duration",
            type=seconds,
            default=None,
            metavar="SECONDS",
            help="time moving to the target or drag start (headless only; default: 0.3; 0 moves immediately; maximum: 300)",
        )

    for gesture in (click, drag):
        gesture.add_argument(
            "--modifier",
            action="append",
            choices=sorted(MODIFIERS),
            type=str.lower,
            default=[],
            help="hold a modifier for the gesture; repeat for combinations",
        )

    typing = commands.add_parser("type", help="type printable Unicode text")
    typing.add_argument("text")
    typing.add_argument(
        "--interval",
        type=seconds,
        default=0.08,
        metavar="SECONDS",
        help="time between characters (default: 0.08; 0 types immediately)",
    )

    key = commands.add_parser(
        "key",
        help="press and release a key or shortcut",
        description="Send a key with optional Ctrl, Shift, and Alt modifiers. "
        "Names are case-insensitive; letter case does not imply Shift. "
        "Keys: a-z, 0-9, Space, Return, Tab, BackSpace, Escape, Delete, Left, Right, Up, Down, "
        "Home, End, Page_Up, Page_Down, F1-F12, and ASCII punctuation names or literals "
        "(comma, period, slash, minus, equal, semicolon, apostrophe, bracketleft, "
        "bracketright, backslash, grave, exclam, quotedbl, numbersign, dollar, percent, "
        "ampersand, parenleft, parenright, asterisk, plus, colon, less, greater, question, "
        "at, asciicircum, underscore, braceleft, bar, braceright, asciitilde). "
        "Use plus or a doubled '+' separator, for example Ctrl+plus or Ctrl++.",
    )
    key.add_argument(
        "chord",
        metavar="CHORD",
        help="for example: Return, Ctrl+a, Ctrl+comma, Ctrl+Shift+z",
    )
    for keyboard_parser in (typing, key):
        keyboard_parser.add_argument(
            "--private",
            action="store_true",
            default=argparse.SUPPRESS,
            help="omit this input's caption and log value",
        )

    args = parser.parse_args()
    if getattr(args, "agent_skill", False):
        parser.error("--agent-skill must be used alone")
    if args.stop_sharing:
        if args.session is not None or args.command is not None:
            parser.error("--stop-sharing takes no session or command")
        raise SystemExit(stop_sharing())
    if args.session is None or args.command is None:
        parser.error(
            "SESSION and COMMAND are required (or use --stop-sharing or --agent-skill)"
        )
    try:
        session_dir = resolve_session_dir(args.session)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.error(f"Cannot resolve the session directory: {error}")
    if args.command == "inspect":
        from framewisp.inspection import Query

        try:
            args.query = Query(
                role=args.role,
                name=args.name,
                text=args.text,
                max_depth=args.max_depth,
                limit=args.limit,
                max_nodes=args.max_nodes,
                timeout=args.timeout,
            )
        except ValueError as error:
            parser.error(str(error))
    if args.command == "batch":
        try:
            with args.file.open("rb") as source:
                content = source.read(MAX_REQUEST_BYTES + 1)
            if len(content) > MAX_REQUEST_BYTES:
                raise ValueError("Batch file exceeds the 1 MiB limit.")
            args.batch = Batch.parse(json.loads(content), directory=Path.cwd())
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
            parser.error(f"Invalid batch: {error}")
    if args.command in {"click", "drag"} and len(set(args.modifier)) != len(
        args.modifier
    ):
        parser.error("each --modifier may only be specified once")
    if args.command == "scroll" and args.ticks < 1:
        parser.error("--ticks must be a positive integer")
    if (
        args.command in {"move", "click", "scroll", "drag"}
        and args.move_duration is not None
        and args.move_duration > 300
    ):
        parser.error("--move-duration must not exceed 300 seconds")
    if args.command == "type" and any(not char.isprintable() for char in args.text):
        parser.error(
            "type supports printable characters only; use key for Return or Tab"
        )
    if args.command == "key" and key_commands(args.chord) is None:
        parser.error(
            "unsupported key combination; use key --help for supported keys and modifiers"
        )
    if args.command in {"run", "record-start"}:
        args.captions = CaptionOptions(
            pointer=args.pointer_captions, key=args.key_captions
        )
        if args.command == "run" and args.captions.enabled and args.record is None:
            parser.error(
                "--pointer-captions and --key-captions require --record; "
                "use record-start for later clips"
            )
        placed = (
            args.console_position is not None
            or args.console_size is not None
            or args.console_cell_scale is not None
        )
        if placed and not args.console:
            parser.error(
                "--console-position, --console-size and --console-cell-scale "
                "require --console"
            )
        args.console_panel = None
        if args.console:
            try:
                args.console_panel = ConsolePanel(
                    args.console_position or DEFAULT_CONSOLE_POSITION,
                    args.console_size or DEFAULT_CONSOLE_SIZE,
                    (
                        DEFAULT_CELL_SCALE
                        if args.console_cell_scale is None
                        else args.console_cell_scale
                    ),
                )
            except ValueError as error:
                parser.error(str(error))
        if args.command == "run" and args.no_click_pings and args.record is None:
            parser.error(
                "--no-click-pings requires --record; use record-start --no-click-pings for later clips"
            )
    if args.command == "run":
        if args.compact and args.record is None:
            parser.error(
                "--compact requires --record; use record-start --compact for later clips"
            )
        if args.console and args.record is None:
            parser.error(
                "--console requires --record; use record-start --console for later clips"
            )
        if not args.app_command or args.app_command == ["--"]:
            parser.error("run requires an app command after --")
        if args.record is not None and (args.width % 2 or args.height % 2):
            parser.error("--record requires even --width and --height")
        layout = video_layout((args.width, args.height), args.console_panel)
        if isinstance(layout, str):
            parser.error(layout)
    state_path = session_dir / "session.json"
    if args.command == "teach":
        if not args.goal.strip():
            parser.error("--goal must not be blank")
        args.directory = args.directory.absolute()
        if args.directory.exists() or args.directory.is_symlink():
            parser.error(
                "Teaching requires a new lesson directory; choose a different path."
            )
        parent = args.directory.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            parser.error(
                f"The lesson's parent must be an existing writable directory: {parent}"
            )
    desktop: DesktopKind | None = None
    if args.command not in {"run", "attach", "teach", "recover"}:
        if args.command == "screenshot" and args.delay:
            # Wait first, so that the checks below see the session the screenshot shows.
            time.sleep(args.delay)
        exit_message = recorded_app_exit(session_dir)
        if exit_message is not None:
            parser.exit(1, exit_message + "\n")
        if not state_path.exists():
            parser.exit(
                1,
                "Session is disconnected. Start run, or ask the user to run attach or teach in another terminal.\n",
            )
        try:
            metadata = json.loads(state_path.read_text())
            protocol = metadata.get("command_protocol")
            published_kind = session_kind(metadata)
        except (OSError, ValueError, RecursionError, AttributeError) as error:
            parser.exit(1, f"Cannot read session metadata: {error}\n")
        # Every session command passes here, so none needs its own check
        # (R-CONTROL-3).
        if protocol != COMMAND_PROTOCOL:
            published = (
                "no command protocol"
                if protocol is None
                else f"command protocol {protocol!r}"
            )
            if published_kind in DESKTOP_KINDS:
                remedy = (
                    "Ask the user to end it with Ctrl+C in its terminal if it is "
                    "still running, then to start it again with this version"
                    + (
                        " and a new lesson directory."
                        if published_kind == "teaching"
                        else "."
                    )
                )
            else:
                remedy = (
                    "Stop its runner with Ctrl+C or SIGTERM, or run framewisp "
                    "SESSION recover if the runner has died, then start the "
                    "session again with this version."
                )
            parser.exit(
                1,
                f"Session {session_dir} was started by another version of framewisp: "
                f"it publishes {published} and this version uses {COMMAND_PROTOCOL}. "
                f"{remedy}\n",
            )
        if published_kind is None:
            parser.exit(
                1,
                f"Cannot read session metadata: {state_path} publishes no known "
                "session kind.\n",
            )
        if published_kind != "headless":
            desktop = published_kind
        if desktop == "teaching" and args.command not in TEACHING_COMMANDS:
            parser.exit(1, f"Session {session_dir}: {teaching_refusal(args.command)}\n")
    try:
        result = dispatch(session_dir, args, desktop=desktop)
    except (SessionError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Session {session_dir}: {error}\n")
    raise SystemExit(result)


def dispatch(
    session_dir: Path, args: argparse.Namespace, *, desktop: DesktopKind | None
) -> int:
    if args.command == "inspect":
        from framewisp.inspection import inspect_session

        observation = inspect_session(session_dir, args.query)
        print(json.dumps(observation, ensure_ascii=False, allow_nan=False))
        result = 0 if observation["status"] == "ok" else 1
    elif args.command == "batch":
        if desktop is not None:
            raise SessionError("batch supports headless sessions only.")
        batch: Batch = args.batch
        result = session_command(session_dir, "batch", parameters=batch.parameters())
    elif args.command in {"attach", "teach"}:
        # Desktop portal imports are only needed by the foreground attach owner.
        from framewisp.attach import attach_session

        if args.command == "teach":
            from framewisp.teaching import Teaching

            result = attach_session(
                session_dir, teaching=Teaching(args.directory, args.goal)
            )
        else:
            result = attach_session(session_dir)
    elif args.command == "teach-note":
        if desktop != "teaching":
            raise SessionError("teach-note requires a teaching session.")
        result = request_desktop(
            session_dir, "teaching", "teach-note", {"text": args.text}
        )
    elif args.command == "recover":
        result = recover_session(session_dir)
    elif args.command == "run":
        app_command: list[str] = args.app_command
        if app_command[:1] == ["--"]:
            app_command = app_command[1:]
        result = run_session(
            session_dir,
            app_command,
            recording=args.record,
            captions=args.captions,
            click_pings=not args.no_click_pings,
            console=args.console_panel,
            compact=args.compact,
            size=(args.width, args.height),
            x11=args.x11,
            real_env=args.real_env,
        )
    elif args.command in {"record-start", "record-stop", "status", "stop"}:
        if desktop is not None and args.command in {"status", "stop"}:
            raise SessionError(
                "status and stop manage headless sessions. The user ends this session with Ctrl+C "
                "in its terminal; framewisp --stop-sharing is the emergency stop for whichever "
                "session is sharing."
            )
        result = (
            request_desktop(session_dir, desktop, args.command, {})
            if desktop is not None
            else session_command(
                session_dir,
                args.command,
                args.path if args.command == "record-start" else None,
                captions=getattr(args, "captions", CaptionOptions()),
                click_pings=not getattr(args, "no_click_pings", False),
                console=getattr(args, "console_panel", None),
                compact=getattr(args, "compact", False),
            )
        )
    elif args.command == "screenshot":
        if desktop is not None and (args.region is not None or args.json):
            raise SessionError(
                "screenshot --region and --json support headless sessions only."
            )
        result = (
            request_desktop(
                session_dir, desktop, "screenshot", {"path": str(args.path.resolve())}
            )
            if desktop is not None
            else screenshot(
                session_dir,
                args.path,
                region=tuple(args.region) if args.region is not None else None,
                metadata=args.json,
            )
        )
    else:
        if args.command in {"move", "click", "scroll", "drag"}:
            if desktop is not None:
                if args.move_duration is not None:
                    raise SessionError(
                        "--move-duration is supported only for headless sessions."
                    )
            elif args.move_duration is None:
                args.move_duration = DEFAULT_MOVE_DURATION
        parameters = {
            key: value
            for key, value in vars(args).items()
            if key not in {"session", "command", "stop_sharing"}
        }
        if desktop is not None:
            parameters.pop("move_duration", None)
            with log_input(session_dir, args.command, parameters) as action:
                result = request_desktop(session_dir, desktop, args.command, parameters)
                action.returncode = result
        else:
            result = session_command(session_dir, args.command, parameters=parameters)
    return result
